#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Created on 14 Aug 2012

@author: Éric Piel
Testing class for pi.py and dacontrol.py .

Copyright © 2012 Éric Piel, Delmic

This file is part of Delmic Acquisition Software.

Delmic Acquisition Software is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 2 of the License, or (at your option) any later version.

Delmic Acquisition Software is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with Delmic Acquisition Software. If not, see http://www.gnu.org/licenses/.
'''
from concurrent import futures
from driver import pigcs
import logging
import math
import os
import time
import unittest

logging.getLogger().setLevel(logging.INFO)


if os.name == "nt":
    PORT = "COM1"
else:
    PORT = "/dev/ttyUSB0"

CONFIG_BUS_BASIC = {"x":(1, 1, False)}
CONFIG_BUS_TWO = {"x":(1, 1, False), "x":(2, 1, False)}  
CONFIG_CTRL_BASIC = (1, {1: False})

#@unittest.skip("faster") 
class TestController(unittest.TestCase):
    """
    directly test the low level class
    """

    def test_scan(self):
        addresses = pigcs.Controller.scan(PORT)
        self.assertGreater(len(addresses), 0, "No controller found")
    
    def test_move(self):
        ser = pigcs.Controller.openSerialPort(PORT)
        ctrl = pigcs.Controller(ser, *CONFIG_CTRL_BASIC)
        speed = ctrl._speed_max / 10
        self.assertGreater(ctrl._speed_max, 100e-6, "Maximum speed is expected to be more than 100μm/s")
        ctrl.setSpeed(1, speed)
        distance = ctrl.moveRel(1, speed/2) # should take 0.5s
        self.assertGreater(distance, 0)
        self.assertTrue(ctrl.isMoving(set([1])))
        self.assertEqual(ctrl.GetErrorNum(), 0)
        status = ctrl.GetStatus()
        time.sleep(1) # a bit more than one second
        self.assertFalse(ctrl.isMoving(set([1])))
        
        # now the same thing but with a stop
        distance = ctrl.moveRel(1, 0.001) # should take one second
        self.assertGreater(distance, 0)
        ctrl.stopMotion()
        time.sleep(0.1)
        self.assertFalse(ctrl.isMoving(set([1])))
        
    def test_timeout(self):
        ser = pigcs.Controller.openSerialPort(PORT)
        ctrl = pigcs.Controller(ser, *CONFIG_CTRL_BASIC)
        
        self.assertIn("Physik Instrumente", ctrl.GetIdentification())
        self.assertTrue(ctrl.IsReady())
        ctrl._sendOrderCommand("\x24") # known to fail
        # the next command is going to have to use recovery from timeout
        self.assertTrue(ctrl.IsReady())
        self.assertEqual(0, ctrl.GetErrorNum())
        
#@unittest.skip("faster")
class TestActuator(unittest.TestCase):
    
    def test_scan(self):
        """
        Check that we can do a scan network. It can pass only if we are
        connected to at least one controller.
        """
        devices = pigcs.Bus.scan()
        self.assertGreater(len(devices), 0)
        
        for name, kwargs in devices:
            print "opening ", name
            stage = pigcs.Bus("test", "stage", None, **kwargs)
            self.assertTrue(stage.selfTest(), "Controller self test failed.")
            
    def test_simple(self):
        stage = pigcs.Bus("test", "stage", None, PORT, CONFIG_BUS_BASIC)
        move = {'x':0.01e-6}
        stage.moveRel(move)
        time.sleep(0.1) # wait for the move to finish
        
    def test_sync(self):
        # For moves big enough, sync should always take more time than async
        delta = 0.0001 # s
        
        stage = pigcs.Bus("test", "stage", None, PORT, CONFIG_BUS_BASIC)
        stage.speed.value = {"x":1e-3}
        move = {'x':100e-6}
        start = time.time()
        f = stage.moveRel(move)
        dur_async = time.time() - start
        f.result()
        self.assertTrue(f.done())
        
        move = {'x':-100e-6}
        start = time.time()
        f = stage.moveRel(move)
        f.result() # wait
        dur_sync = time.time() - start
        self.assertTrue(f.done())
        
        self.assertGreater(dur_sync, max(0, dur_async - delta), "Sync should take more time than async.")
        
        move = {'x':100e-6}
        f = stage.moveRel(move)
        # timeout = 0.001s should be too short for such a long move
        self.assertRaises(futures.TimeoutError, f.result, timeout=0.001)
        

    def test_speed(self):
        # For moves big enough, a 0.1m/s move should take approximately 100 times less time
        # than a 0.001m/s move 
        expected_ratio = 10.0
        delta_ratio = 2.0 # no unit 
        
        # fast move
        stage = pigcs.Bus("test", "stage", None, PORT, CONFIG_BUS_BASIC)
        stage.speed.value = {"x":0.001} # max speed of E-861 in practice
        move = {'x':1e-3}
        start = time.time()
        f = stage.moveRel(move)
        f.result()
        dur_fast = time.time() - start
        act_speed = abs(move['x']) / dur_fast
        print "actual speed=%f" % act_speed
        ratio = act_speed / stage.speed.value['x']
        if delta_ratio/2 < ratio or ratio > delta_ratio:
            self.fail("Speed not consistent: %f m/s instead of %f m/s." %
                      (act_speed, stage.speed.value['x']))
        
        stage.speed.value = {"x":0.001/expected_ratio}
        move = {'x':-1e-3}
        start = time.time()
        f = stage.moveRel(move)
        f.result()
        dur_slow = time.time() - start
        act_speed = abs(move['x']) / dur_slow
        print "actual speed=%f" % act_speed
        ratio = act_speed / stage.speed.value['x']
        if delta_ratio/2 < ratio or ratio > delta_ratio:
            self.fail("Speed not consistent: %f m/s instead of %f m/s." %
                      (act_speed, stage.speed.value['x']))
                    
        ratio = dur_slow / dur_fast
        print "ratio of %f while expected %f" % (ratio, expected_ratio)
        if ratio < expected_ratio / 2 or ratio > expected_ratio * 2:
            self.fail("Speed not consistent: ratio of " + str(ratio) + 
                         " instead of " + str(expected_ratio) + ".")

    def test_stop(self):
        stage = pigcs.Bus("test", "stage", None, PORT, CONFIG_BUS_BASIC)
        stage.stop()
        
        move = {'x':100e-6}
        f = stage.moveRel(move)
        stage.stop()
        self.assertTrue(f.cancelled())
    
    def test_queue(self):
        """
        Ask for several long moves in a row, and checks that nothing breaks
        """
        stage = pigcs.Bus("test", "stage", None, PORT, CONFIG_BUS_BASIC)
        move_forth = {'x':1e-3}
        move_back = {'x':-1e-3}
        stage.speed.value = {"x":1e-3} # => 1s per move
        start = time.time()
        expected_time = 4 * move_forth["x"] / stage.speed.value["x"]
        f0 = stage.moveRel(move_forth)
        f1 = stage.moveRel(move_back)
        f2 = stage.moveRel(move_forth)
        f3 = stage.moveRel(move_back)
        
        # intentionally skip some sync (it _should_ not matter)
#        f0.result()
        f1.result()
#        f2.result()
        f3.result()
        
        dur = time.time() - start
        self.assertGreaterEqual(dur, expected_time)
    
    def test_cancel(self):
        stage = pigcs.Bus("test", "stage", None, PORT, CONFIG_BUS_BASIC)
        move_forth = {'x':1e-3}
        move_back = {'x':-1e-3}
        stage.speed.value = {"x":1e-3} # => 1s per move
        # test cancel during action
        f = stage.moveRel(move_forth)
        time.sleep(0.01) # to make sure the action is being handled
        self.assertTrue(f.running())
        f.cancel()
        self.assertTrue(f.cancelled())
        self.assertTrue(f.done())
        
        # test cancel in queue
        f1 = stage.moveRel(move_forth)
        f2 = stage.moveRel(move_back)
        f2.cancel()
        self.assertFalse(f1.done())
        self.assertTrue(f2.cancelled())
        self.assertTrue(f2.done())
        
        # test cancel after already cancelled
        f.cancel()
        self.assertTrue(f.cancelled())
        self.assertTrue(f.done())
        
        f1.result() # wait for the move to be finished
        
    def test_not_cancel(self):
        stage = pigcs.Bus("test", "stage", None, PORT, CONFIG_BUS_BASIC)
        small_move_forth = {'x':1e-4}
        stage.speed.value = {"x":1e-3} # => 0.1s per move
        # test cancel after done => not cancelled
        f = stage.moveRel(small_move_forth)
        time.sleep(1)
        self.assertFalse(f.running())
        f.cancel()
        self.assertFalse(f.cancelled())
        self.assertTrue(f.done())
        
        # test cancel after result()
        f = stage.moveRel(small_move_forth)
        f.result()
        f.cancel()
        self.assertFalse(f.cancelled())
        self.assertTrue(f.done())
        
        # test not cancelled
        f = stage.moveRel(small_move_forth)
        f.result()
        self.assertFalse(f.cancelled())
        self.assertTrue(f.done())
    
    def test_move_circle(self):
        # check if we can run it
        devices = pigcs.Bus.scan(PORT)
        if len(devices) < 2:
            self.skipTest("Couldn't find two controllers")
        
        stage = pigcs.Bus("test", "stage", None, PORT, CONFIG_BUS_TWO)
        stage.speed.value = {"x":1e-3, "y":1e-3}
        radius = 100e-6 # m
        # each step has to be big enough so that each move is above imprecision
        steps = 100
        cur_pos = (0, 0)
        move = {}
        for i in xrange(steps):
            next_pos = (radius * math.cos(2 * math.pi * float(i) / steps),
                        radius * math.sin(2 * math.pi * float(i) / steps))
            move['x'] = next_pos[0] - cur_pos[0]
            move['y'] = next_pos[1] - cur_pos[1]
            print next_pos, move
            f = stage.moveRel(move)
            f.result() # wait
            cur_pos = next_pos

    def test_future_callback(self):
        stage = pigcs.Bus("test", "stage", None, PORT, CONFIG_BUS_BASIC)
        move_forth = {'x':1e-4}
        move_back = {'x':-1e-4}
        stage.speed.value = {"x":1e-3} # => 0.1s per move
        
        # test callback while being executed
        f = stage.moveRel(move_forth)
        self.called = 0
        time.sleep(0.01)
        f.add_done_callback(self.callback_test_notify)
        f.result()
        time.sleep(0.01) # make sure the callback had time to be called
        self.assertEquals(self.called, 1)
        self.assertTrue(f.done())

        # test callback while in the queue
        f1 = stage.moveRel(move_back)
        f2 = stage.moveRel(move_forth)
        f2.add_done_callback(self.callback_test_notify)
        self.assertFalse(f1.done())
        f2.result()
        self.assertTrue(f1.done())
        time.sleep(0.01) # make sure the callback had time to be called
        self.assertEquals(self.called, 2)
        self.assertTrue(f2.done())

        # It should work even if the action is fully done
        f2.add_done_callback(self.callback_test_notify2)
        self.assertEquals(self.called, 3)
        
        # test callback called after being cancelled
        f = stage.moveRel(move_forth)
        self.called = 0
        time.sleep(0.01)
        f.add_done_callback(self.callback_test_notify)
        f.cancel()
        time.sleep(0.01) # make sure the callback had time to be called
        self.assertEquals(self.called, 1)
        self.assertTrue(f.cancelled()) 
        
    def callback_test_notify(self, future):
        self.assertTrue(future.done())
        self.called += 1
        
    def callback_test_notify2(self, future):
        self.assertTrue(future.done())
        self.called += 1
        
if __name__ == "__main__":
    #import sys;sys.argv = ['', 'Test.testName']
    unittest.main()