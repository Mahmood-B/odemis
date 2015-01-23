# -*- coding: utf-8 -*-
'''
Created on 22 Jan 2015

@author: Éric Piel

Copyright © 2014 Éric Piel, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License version 2 as published by the Free Software Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with Odemis. If not, see http://www.gnu.org/licenses/.
'''

# Driver for New Focus (from New Port) picomotor controller 874x.
# Currently only 8742 over IP is supported. The documentation is
# available on newport.com (8742_User_Manual_revB.pdf).

# Note that the IP scanning protocol requires to listen on port 23 (telnet).
# This is typically not allowed for standard user. That is why the scanner is
# in a separate executable. This allows to give special privileges (eg, via
# authbind) to just this small executable.

from __future__ import division

from concurrent.futures._base import CancelledError
import logging
import numpy
from odemis import model
import odemis
from odemis.model import (isasync, CancellableThreadPoolExecutor,
                          CancellableFuture, HwError)
import os
import re
import socket
import struct
from subprocess import CalledProcessError
import subprocess
import threading
import time


class NewFocusError(Exception):
    def __init__(self, errno, strerror):
        self.args = (errno, strerror)
        self.errno = errno
        self.strerror = strerror

    def __str__(self):
        return "%d: %s" % (self.errno, self.strerror)

# Motor types
MT_NONE = 0
MT_UNKNOWN = 1
MT_TINY = 2
MT_STANDARD = 3

class PM8742(model.Actuator):
    """
    Represents one New Focus picomotor controller 8742.
    """
    def __init__(self, name, role, address, axes, stepsize, sn=None, **kwargs):
        """
        address (str): ip address (use "autoip" to automatically scan and find the
          controller, "fake" for a simulator)
        axes (list of str): names of the axes, from the 1st to the 4th, if present.
          if an axis is not connected, put a "".
        stepsize (list of float): size of a step in m (the smaller, the
          bigger will be a move for a given distance in m)
        sn (str or None): serial number of the device (eg, "11500"). If None, the
          driver will use whichever controller is first found.
        inverted (set of str): names of the axes which are inverted (IOW, either
         empty or the name of the axis)
        """
        if not 1 <= len(axes) <= 4:
            raise ValueError("Axes must be a list of 1 to 4 axis names (got %s)" % (axes,))
        if len(axes) != len(stepsize):
            raise ValueError("Expecting %d stepsize (got %s)" %
                             (len(axes), stepsize))
        self._name_to_axis = {} # str -> int: name -> axis number
        for i, n in enumerate(axes):
            if n == "": # skip this non-connected axis
                continue
            self._name_to_axis[n] = i + 1

        for sz in stepsize:
            if sz > 10e-3: # sz is typically ~1µm, so > 1 cm is very fishy
                raise ValueError("stepsize should be in meter, but got %g" % (sz,))
        self._stepsize = stepsize

        self._accesser = self._openConnection(address, sn)

        self._resynchonise()

        if name is None and role is None: # For scan only
            return

        modl, fw, sn = self.GetIdentification()
        if modl != "8742":
            logging.warning("Controller %s is not supported, will try anyway", modl)

        # will take care of executing axis move asynchronously
        self._executor = CancellableThreadPoolExecutor(max_workers=1) # one task at a time

        # Let the controller check the actuators are connected
        self.MotorCheck()

        axes_def = {}
        for n, i in self._name_to_axis.items():
            sz = self._stepsize[i-1]
            # TODO: allow to pass the range in m in the arguments
            # Position supports ±2³¹, probably not that much in reality, but
            # there is no info.
            rng = [(-2 ** 31) * sz, (2 ** 31 - 1) * sz]
            axes_def[n] = model.Axis(range=rng, unit="m")

            # Check the actuator is connected
            mt = self.GetMotorType(i)
            if mt in {MT_NONE, MT_UNKNOWN}:
                raise HwError("Controller failed to detect motor %d, check the "
                              "actuator is connected to the controller" %
                              (i,))

        model.Actuator.__init__(self, name, role, axes=axes_def, **kwargs)

        self._swVersion = "%s (IP connection)" % (odemis.__version__,)
        self._hwVersion = "New Focus %s (firmware %s, S/N %s)" % (modl, fw, sn)

        # Note that the "0" position is just the position at which the
        # controller turned on
        self.position = model.VigilantAttribute({}, unit="m", readonly=True)
        self._updatePosition()

        # TODO: add support for changing speed
        self.speed = model.VigilantAttribute({}, unit="m/s", readonly=True)
        self._updateSpeed()

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown(wait=True)
            self._executor = None
        
        if self._accesser:
            self._accesser.terminate()
            self._accesser = None

    # Low level functions
    def GetIdentification(self):
        """
        return (str, str, str): 
             Model name
             Firmware version (and date)
             serial number
        """
        resp = self._accesser.sendQueryCommand("*IDN")
        # expects something like this:
        # New_Focus 8742 v2.2 08/01/13 11511
        try:
            m = re.match("\w+ (?P<model>\w+) (?P<fw>v\S+ \S+) (?P<sn>\d+)", resp)
            modl, fw, sn = m.group("model"), m.group("fw"), m.group("sn")
        except Exception:
            raise IOError("Failed to decode firmware answer '%s'" %
                          resp.encode('string_escape'))

        return modl, fw, sn

    def GetMotorType(self, axis):
        """
        Read the motor type.
        The motor check action must have been performed before to get correct
          values. 
        axis (1<=int<=4): axis number
        return (0<=int<=3): the motor type
        """
        resp = self._accesser.sendQueryCommand("QM", axis=axis)
        return int(resp)

    def GetVelocity(self, axis):
        """
        Read the max speed
        axis (1<=int<=4): axis number
        return (0<=int<=2000): the speed in step/s
        """
        resp = self._accesser.sendQueryCommand("VA", axis=axis)
        return int(resp)

    def SetVelocity(self, axis, val):
        """
        Write the max speed
        axis (1<=int<=4): axis number
        val (1<=int<=2000): the speed in step/s
        """
        if not 1 <= val <= 2000:
            raise ValueError("Velocity outside of the range 0->2000")
        self._accesser.sendOrderCommand("VA", "%d" % (val,), axis)

    def GetAccel(self, axis):
        """
        Read the acceleration
        axis (1<=int<=4): axis number
        return (0<=int): the acceleration in step/s²
        """
        resp = self._accesser.sendQueryCommand("AC", axis=axis)
        return int(resp)

    def SetAccel(self, axis, val):
        """
        Write the acceleration
        axis (1<=int<=4): axis number
        val (1<=int<=200000): the acceleration in step/s²
        """
        if not 1 <= val <= 200000:
            raise ValueError("Acceleration outside of the range 0->200000")
        self._accesser.sendOrderCommand("AC", "%d" % (val,), axis)

    def MotorCheck(self):
        """
        Run the motor check command, that automatically configure the right
        values based on the type of motors connected.
        """
        self._accesser.sendOrderCommand("MC")

    def MoveAbs(self, axis, pos):
        """
        Requests a move to an absolute position. This is non-blocking.
        axis (1<=int<=4): axis number
        pos (-2**31 <= int 2*31-1): position in step
        """
        self._accesser.sendOrderCommand("PA", "%d" % (pos,), axis)

    def GetTarget(self, axis):
        """
        Read the target position for the given axis
        axis (1<=int<=4): axis number
        return (int): the position in steps
        """
        # Note, it's not clear what's the difference with PR?
        resp = self._accesser.sendQueryCommand("PA", axis=axis)
        return int(resp)

    def MoveRel(self, axis, offset):
        """
        Requests a move to a relative position. This is non-blocking.
        axis (1<=int<=4): axis number
        offset (-2**31 <= int 2*31-1): offset in step
        """
        self._accesser.sendOrderCommand("PR", "%d" % (offset,), axis)

    def GetPosition(self, axis):
        """
        Read the actual position for the given axis
        axis (1<=int<=4): axis number
        return (int): the position in steps
        """
        resp = self._accesser.sendQueryCommand("TP", axis=axis)
        return int(resp)

    def IsMoving(self, axis):
        """
        Check whether the axis is in motion 
        axis (1<=int<=4): axis number
        return (bool): True if in motion
        """
        resp = self._accesser.sendQueryCommand("MD", axis=axis)
        if resp == "0": # motion in progress
            return True
        elif resp == "1": # no motion
            return False
        else:
            raise IOError("Failed to decode answer about motion '%s'" %
                          resp.encode('string_escape'))

    def AbortMotion(self, axis):
        """
        Stop immediatelly the motion on all the axes
        """
        self._accesser.sendOrderCommand("AB")

    def StopMotion(self, axis):
        """
        Stop nicely the motion (using accel/decel values)
        axis (1<=int<=4): axis number
        """
        self._accesser.sendOrderCommand("ST", axis=axis)

    def GetError(self):
        """
        Read the oldest error in memory.
        The error buffer is FIFO with 10 elements, so it might not be the 
        latest error if multiple errors have happened since the last time this
        function was called.
        return (None or (int, str)): the error number and message
        """
        # Note: there is another one "TE" which only returns the number, and so
        # is faster, but then there is no way to get the message
        resp = self._accesser.sendQueryCommand("TB")
        # returns something like "108, MOTOR NOT CONNECTED"
        try:
            m = re.match("(?P<no>\d+), (?P<msg>.+)", resp)
            no, msg = int(m.group("no")), m.group("msg")
        except Exception:
            raise IOError("Failed to decode error info '%s'" %
                          resp.encode('string_escape'))

        if no == 0:
            return None
        else:
            return no, msg

    # TODO: make a check error function that raises NewFocusError if needed

    def _resynchonise(self):
        """
        Ensures the device communication is "synchronised"
        """
        self._accesser.flushInput()

        # drop all the errors
        while self.GetError():
            pass


    # high-level methods (interface)
    def _updatePosition(self, axes=None):
        """
        update the position VA
        axes (set of str): names of the axes to update or None if all should be
          updated
        """
        pos = self.position.value
        for n, i in self._name_to_axis.items():
            if axes is None or n in axes:
                pos[n] = self.GetPosition(i) * self._stepsize[i - 1]

        # it's read-only, so we change it via _value
        self.position._value = pos
        self.position.notify(self.position.value)
    
    def _updateSpeed(self):
        """
        Update the speed VA from the controller settings
        """
        speed = {}
        for n, i in self._name_to_axis.items():
            speed[n] = self.GetVelocity(i) * self._stepsize[i - 1]

        # TODO: make it read/write
        # it's read-only, so we change it via _value
        self.speed._value = speed
        self.speed.notify(self.speed.value)

    def _createFuture(self):
        """
        Return (CancellableFuture): a future that can be used to manage a move
        """
        f = CancellableFuture()
        f._moving_lock = threading.Lock() # taken while moving
        f._must_stop = threading.Event() # cancel of the current future requested
        f._was_stopped = False # if cancel was successful
        f.task_canceller = self._cancelCurrentMove
        return f

    @isasync
    def moveRel(self, shift):
        self._checkMoveRel(shift)
        shift = self._applyInversionRel(shift)
        
        # Check if the distance is big enough to make sense
        for an, v in shift.items():
            aid = self._name_to_axis[an]
            if abs(v) < self._stepsize[aid - 1]:
                # TODO: store and accumulate all the small moves instead of dropping them?
                del shift[an]
                logging.info("Dropped too small move of %f m", abs(v))
        
        if not shift:
            return model.InstantaneousFuture()

        f = self._createFuture()
        f = self._executor.submitf(f, self._doMoveRel, f, shift)
        return f

    @isasync
    def moveAbs(self, pos):
        if not pos:
            return model.InstantaneousFuture()
        self._checkMoveAbs(pos)
        pos = self._applyInversionRel(pos)

        f = self._createFuture()
        f = self._executor.submitf(f, self._doMoveAbs, f, pos)
        return f
    moveAbs.__doc__ = model.Actuator.moveAbs.__doc__

    def stop(self, axes=None):
        self._executor.cancel()

    def _doMoveRel(self, future, pos):
        """
        Blocking and cancellable relative move
        future (Future): the future it handles
        pos (dict str -> float): axis name -> relative target position
        """
        with future._moving_lock:
            end = 0 # expected end
            moving_axes = set()
            for an, v in pos.items():
                aid = self._name_to_axis[an]
                moving_axes.add(aid)
                steps = int(round(v / self._stepsize[aid - 1]))
                self.MoveRel(aid, steps)
                # compute expected end
                dur = abs(steps) * self._stepsize[aid - 1] / self.speed.value[an]
                end = max(time.time() + dur, end)

            self._waitEndMove(future, moving_axes, end)
        logging.debug("move successfully completed")

    def _doMoveAbs(self, future, pos):
        """
        Blocking and cancellable absolute move
        future (Future): the future it handles
        pos (dict str -> float): axis name -> absolute target position
        """
        with future._moving_lock:
            end = 0 # expected end
            old_pos = self.position.value
            moving_axes = set()
            for an, v in pos.items():
                aid = self._name_to_axis[an]
                moving_axes.add(aid)
                steps = int(round(v / self._stepsize[aid - 1]))
                self.MoveAbs(aid, steps)
                # compute expected end
                dur = abs(v - old_pos[an]) / self.speed.value[an]
                end = max(time.time() + dur, end)

            self._waitEndMove(future, moving_axes, end)
        logging.debug("move successfully completed")

    def _waitEndMove(self, future, axes, end=0):
        """
        Wait until all the given axes are finished moving, or a request to 
        stop has been received.
        future (Future): the future it handles
        axes (set of int): the axes IDs to check
        end (float): expected end time
        raise:
            CancelledError: if cancelled before the end of the move
        """
        moving_axes = set(axes)

        last_upd = time.time()
        last_axes = moving_axes.copy()
        try:
            while not future._must_stop.is_set():
                for aid in moving_axes.copy(): # need copy to remove during iteration
                    if not self.IsMoving(aid):
                        moving_axes.discard(aid)
                if not moving_axes:
                    # no more axes to wait for
                    return

                # Update the position from time to time (10 Hz)
                if time.time() - last_upd > 0.1 or last_axes != moving_axes:
                    last_names = set(n for n, i in self._name_to_axis.items() if i in last_axes)
                    self._updatePosition(last_names)
                    last_upd = time.time()
                    last_axes = moving_axes.copy()

                # Wait half of the time left (maximum 0.1 s)
                left = end - time.time()
                sleept = max(0, min(left / 2, 0.1))
                future._must_stop.wait(sleept)

            logging.debug("Move of axes %s cancelled before the end", axes)
            # stop all axes still moving them
            for i in moving_axes:
                self.StopMotion(i)
            future._was_stopped = True
            raise CancelledError()
        finally:
            self._updatePosition() # update (all axes) with final position

    def _cancelCurrentMove(self, future):
        """
        Cancels the current move (both absolute or relative). Non-blocking.
        future (Future): the future to stop. Unused, only one future must be 
         running at a time.
        return (bool): True if it successfully cancelled (stopped) the move.
        """
        # The difficulty is to synchronise correctly when:
        #  * the task is just starting (not finished requesting axes to move)
        #  * the task is finishing (about to say that it finished successfully)
        logging.debug("Cancelling current move")

        future._must_stop.set() # tell the thread taking care of the move it's over
        with future._moving_lock:
            if not future._was_stopped:
                logging.debug("Cancelling failed")
            return future._was_stopped

    @classmethod
    def scan(cls):
        """
        returns (list of (str, dict)): name, kwargs
        Note: it's obviously not advised to call this function if a device is already under use
        """
        logging.info("Scanning for TMCM controllers in progress...")
        found = []  # (list of 2-tuple): name, kwargs
        try:
            conts = cls._scanOverIP()
        except IOError as exp:
            logging.exception("Failed to scan for New Focus controllers: %s", exp)

        for hn, host, port in conts:
            try:
                logging.debug("Trying controller at %s", host)
                dev = cls(None, None, address=host, axes=["a"], stepsize=[1e-6])
                modl, fw, sn = dev.GetIdentification()

                # find out about the axes
                dev.MotorCheck()
                axes = []
                stepsize = []
                for i in range(1, 5):
                    mt = dev.GetMotorType(i)
                    n = chr(ord('a') + i - 1)
                    # No idea about the stepsize, but make it different to allow
                    # distinguishing between motor types
                    if mt == MT_STANDARD:
                        ss = 10e-6
                    elif mt == MT_TINY:
                        ss = 1e-6
                    else:
                        n = ""
                        ss = 0
                    axes.append(n)
                    stepsize.append(ss)
            except IOError:
                # not possible to use this port? next one!
                continue
            except Exception:
                logging.exception("Error while communicating with controller %s @ %s:%s",
                                  hn, host, port)
                continue

            found.append(("TMCM-%s" % modl,
                          {"address": host,
                           "axes": axes,
                           "stepsize": stepsize,
                           "sn": sn})
                        )

        return found

    @classmethod
    def _openConnection(cls, address, sn=None):
        """
        return (Accesser)
        """
        if address == "fake":
            host, port = "fake", 23
        elif address == "autoip":
            conts = cls._scanOverIP()
            if sn is not None:
                for hn, host, port in conts:
                    # Open connection to each controller and ask for their SN
                    dev = cls(None, None, address=host, axes=["a"], stepsize=[1e-6])
                    _, _, devsn = dev.GetIdentification()
                    if sn == devsn:
                        break
                else:
                    raise HwError("Failed to find New Focus controller %s over the "
                                  "network. Ensure it is turned on and connected to "
                                  "the network." % (sn))
            else:
                # just pick the first one
                # TODO: only pick the ones of model 8742
                try:
                    hn, host, port = conts[0]
                    logging.info("Connecting to New Focus %s", hn)
                except IndexError:
                    raise HwError("Failed to find New Focus controller over the "
                                  "network. Ensure it is turned on and connected to "
                                  "the network.")

        else:
            # split the (IP) port, separated by a :
            if ":" in address:
                host, ipport_str = port.split(":")
                port = int(ipport_str)
            else:
                host = address
                port = 23 # default

        return IPAccesser(host, port)

    @staticmethod
    def _scanOverIP():
        """
        Scan the network for all the responding new focus controllers
        Note: it actually calls a separate executable because it relies on opening
          a network port which needs special privileges.
        return (list of (str, str, int)): hostname, ip address, and port number
        """
        # Run the separate program via authbind
        try:
            exc = os.path.join(os.path.dirname(__file__), "nfpm_netscan.py")
            out = subprocess.check_output(["authbind", "python", exc])
        except CalledProcessError as exp:
            # and handle all the possible errors:
            # - no authbind (127)
            # - cannot find the separate program (2)
            # - no authorisation (13)
            ret = exp.returncode
            if ret == 127:
                raise IOError("Failed to find authbind")
            elif ret == 2:
                raise IOError("Failed to find %s" % exc)
            elif ret == 13:
                raise IOError("No permission to open network port 23")

        # or decode the output
        # hostname \t host \t port
        ret = []
        for l in out.split("\n"):
            if not l:
                continue
            try:
                hn, host, port = l.split("\t")
            except Exception:
                logging.exception("Failed to decode scanner line '%s'", l)
            ret.append((hn, host, port))

        return ret

class IPAccesser(object):
    """
    Manages low-level connections over IP
    """
    def __init__(self, host, port=23):
        """
        host (string): the IP address or host name of the master controller
        port (int): the (IP) port number
        """
        self._host = host
        self._port = port
        if host == "fake":
            self.socket = PM8742Simulator()
        else:
            try:
                self.socket = socket.create_connection((host, port), timeout=5)
            except socket.timeout:
                raise model.HwError("Failed to connect to '%s:%d', check the New Focus "
                                    "controller is connected to the network, turned "
                                    " on, and correctly configured." % (host, port))

        self.socket.settimeout(1.0) # s

        # it always sends '\xff\xfd\x03\xff\xfb\x01' on a new connection
        # => discard it
        try:
            data = self.socket.recv(100)
        except socket.timeout:
            logging.debug("Didn't receive any welcome message")
        
        # to acquire before sending anything on the socket
        self._net_access = threading.Lock()

    def terminate(self):
        self.socket.close()

    def sendOrderCommand(self, cmd, val="", axis=None):
        """
        Sends one command, and don't expect any reply
        cmd (str): command to send
        val (str): value to send (if any) 
        axis (1<=int<=4 or None): axis number
        raises:
            IOError: if problem with sending/receiving data over the connection
            NewFocusError: if error happened
        """
        if axis is None:
            str_axis = ""
        else:
            str_axis = "%d" % axis

        if not 1 <= len(cmd) <= 10:
            raise ValueError("Command %s is very likely wrong" % (cmd,))

        # Note: it also accept a N> prefix to specify the controller number,
        # but we don't support multiple controllers (for now)
        msg = "%s%s%s\r" % (str_axis, cmd, val)

        with self._net_access:
            logging.debug("Sending: '%s'", msg.encode('string_escape'))
            self.socket.sendall(msg)

    def sendQueryCommand(self, cmd, val="", axis=None):
        """
        Sends one command, and don't expect any reply
        cmd (str): command to send, without ?
        val (str): value to send (if any) 
        axis (1<=int<=4 or None): axis number
        raises:
            IOError: if problem with sending/receiving data over the connection
            NewFocusError: if error happened
        """
        if axis is None:
            str_axis = ""
        else:
            str_axis = "%d" % axis

        if not 1 <= len(cmd) <= 10:
            raise ValueError("Command %s is very likely wrong" % (cmd,))

        # Note: it also accept a N> prefix to specify the controller number,
        # but we don't support multiple controllers (for now)
        msg = "%s%s?%s\r" % (str_axis, cmd, val)

        with self._net_access:
            logging.debug("Sending: '%s'", msg.encode('string_escape'))
            self.socket.sendall(msg)

            # read the answer
            end_time = time.time() + 0.5
            ans = ""
            while True:
                try:
                    data = self.socket.recv(4096)
                except socket.timeout:
                    raise HwError("Controller %s timed out after %s" %
                                  (self._host, msg.encode('string_escape')))

                if not data:
                    logging.debug("Received empty message")

                ans += data
                # does it look like we received a full answer?
                if len(ans) >= 3 and ans[-2:] == "\r\n":
                    break

                if time.time() > end_time:
                    raise IOError("Controller %s timed out after %s" %
                                  (self._host, msg.encode('string_escape')))
                time.sleep(0.01)

        logging.debug("Received: %s", ans.encode('string_escape'))
        return ans[:-2] # remove the end of line characters

    def flushInput(self):
        """
        Ensure there is no more data queued to be read on the bus
        """
        with self._net_access:
            try:
                while True:
                    data = self.socket.recv(4096)
            except socket.timeout:
                pass
            except Exception:
                logging.exception("Failed to flush correctly the socket")


class PM8742Simulator(object):
    """
    Simulates a PM8742 (+ socket connection). Only used for testing.
    Same interface as the network socket
    """
    def __init__(self):
        self._output_buf = "" # what the commands sends back to the "host computer"
        self._input_buf = "" # what we receive from the "host computer"

        self._naxes = 4

        # internal state
        self._id = 1

        # internal global param values
        # 4 * dict(int -> int: param number -> value)
        self._gstate = [{}, {}, {}, {}]

        # internal axis param values
        # int -> int: param number -> value
        orig_axis_state = {0: 0, # target position
                           1: 0, # current position (unused directly)
                           4: 1024, # maximum positioning speed
                           8: 1, # target reached? (unused directly)
                           154: 3, # pulse div
                           }
        self._astates = [dict(orig_axis_state) for i in range(self._naxes)]

        # (float, float, int) for each axis 
        # start, end, start position of a move
        self._axis_move = [(0,0,0)] * self._naxes

    def _getCurrentPos(self, axis):
        """
        return (int): position in microsteps
        """
        now = time.time()
        startt, endt, startp = self._axis_move[axis]
        endp = self._astates[axis][0]
        if endt < now:
            return endp
        # model as if it was linear (it's not, it's ramp-based positioning)
        pos = startp + (endp - startp) * (now - startt) / (endt - startt)
        return pos

    def _getMaxSpeed(self, axis):
        """
        return (float): speed in microsteps/s
        """
        velocity = self._astates[axis][4]
        pulse_div = self._astates[axis][154]
        usf = (16e6 * velocity) / (2 ** pulse_div * 2048 * 32)
        return usf # µst/s

    def write(self, data):
        # We accept both a string/bytes and numpy array
        if isinstance(data, numpy.ndarray):
            data = data.tostring()
        self._input_buf += data

        # each message is 9 bytes => take the first 9 and process them
        while len(self._input_buf) >= 9:
            msg = self._input_buf[:9]
            self._input_buf = self._input_buf[9:]
            self._parseMessage(msg) # will update _output_buf

    def read(self, size=1):
        ret = self._output_buf[:size]
        self._output_buf = self._output_buf[len(ret):]

        if len(ret) < size:
            # simulate timeout
            time.sleep(self.timeout)
        return ret

    def flush(self):
        pass

    def flushInput(self):
        self._output_buf = ""

    def close(self):
        # using read or write will fail after that
        del self._output_buf
        del self._input_buf

    def _sendReply(self, inst, status=100, val=0):
        msg = numpy.empty(9, dtype=numpy.uint8)
        struct.pack_into('>BBBBiB', msg, 0, 2, self._id, status, inst, val, 0)
        # compute the checksum (just the sum of all the bytes)
        msg[-1] = numpy.sum(msg[:-1], dtype=numpy.uint8)

        self._output_buf += msg.tostring()
        
    def _parseMessage(self, msg):
        """
        msg (buffer of length 9): the message to parse
        return None: self._output_buf is updated if necessary
        """
        target, inst, typ, mot, val, chk = struct.unpack('>BBBBiB', msg)
#         logging.debug("SIM: parsing %s", TMCM3110._instr_to_str(msg))

        # Check it's a valid message... for us
        npmsg = numpy.frombuffer(msg, dtype=numpy.uint8)
        good_chk = numpy.sum(npmsg[:-1], dtype=numpy.uint8)
        if chk != good_chk:
            self._sendReply(inst, status=1) # "Wrong checksum" message
            return
        if target != self._id:
            logging.warning("SIM: skipping message for %d", target)
            # The real controller doesn't seem to care

        # decode the instruction
        if inst == 3: # Motor stop
            if not 0 <= mot <= self._naxes:
                self._sendReply(inst, status=4) # invalid value
                return
            # Note: the target position in axis param is not changed (in the
            # real controller)
            self._axis_move[mot] = (0, 0, 0)
            self._sendReply(inst)
        elif inst == 4: # Move to position
            if not 0 <= mot <= self._naxes:
                self._sendReply(inst, status=4) # invalid value
                return
            if not typ in [0, 1, 2]:
                self._sendReply(inst, status=3) # wrong type
                return
            pos = self._getCurrentPos(mot)
            if typ == 1: # Relative
                # convert to absolute and continue
                val += pos
            elif typ == 2: # Coordinate
                raise NotImplementedError("simulator doesn't support coordinates")
            # new move
            now = time.time()
            end = now + abs(pos - val) / self._getMaxSpeed(mot)
            self._astates[mot][0] = val
            self._axis_move[mot] = (now, end, pos)
            self._sendReply(inst, val=val)
        elif inst == 5: # Set axis parameter
            if not 0 <= mot <= self._naxes:
                self._sendReply(inst, status=4) # invalid value
                return
            if not 0 <= typ <= 255:
                self._sendReply(inst, status=3) # wrong type
                return
            # Warning: we don't handle special addresses
            if typ == 1: # actual position
                self._astates[mot][0] = val # set target position, which will be used for current pos
            else:
                self._astates[mot][typ] = val
            self._sendReply(inst, val=val)
        elif inst == 6: # Get axis parameter
            if not 0 <= mot <= self._naxes:
                self._sendReply(inst, status=4) # invalid value
                return
            if not 0 <= typ <= 255:
                self._sendReply(inst, status=3) # wrong type
                return
            # special code for special values
            if typ == 1: # actual position
                rval = self._getCurrentPos(mot)
            elif typ == 8: # target reached?
                rval = 0 if self._axis_move[mot][1] > time.time() else 1
            else:
                rval = self._astates[mot].get(typ, 0) # default to 0
            self._sendReply(inst, val=rval)
        elif inst == 15: # Get IO
            if not 0 <= mot <= 2:
                self._sendReply(inst, status=4) # invalid value
                return
            if not 0 <= typ <= 7:
                self._sendReply(inst, status=3) # wrong type
                return
            if mot == 0: # digital inputs
                rval = 0 # between 0..1
            elif mot == 1: # analogue inputs
                rval = 178 # between 0..4095
            elif mot == 2: # digital outputs
                rval = 0 # between 0..1
            self._sendReply(inst, val=rval)
        elif inst == 136: # Get firmware version
            if typ == 0: # string
                raise NotImplementedError("Can't simulated GFV string")
            elif typ == 1: # binary
                self._sendReply(inst, val=0x0c260102) # 3110 v1.02
            else:
                self._sendReply(inst, status=3) # wrong type
        elif inst == 138: # Request Target Position Reached Event
            raise NotImplementedError("Can't simulated RTP string")
        else:
            logging.warning("SIM: Unsupported instruction %d", inst)
            self._sendReply(inst, status=2) # wrong instruction
