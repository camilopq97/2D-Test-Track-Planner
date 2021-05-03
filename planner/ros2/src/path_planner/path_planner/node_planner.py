#!/usr/bin/env python3
# =============================================================================
"""
Code Information:
    Maintainer: John Alberto Betancourt G
	Mail: john@kiwicampus.com
	Kiwi Campus / Computer & Ai Vision Team
"""

# =============================================================================
import numpy as np
import yaml
import csv
import sys
import os

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.logging import get_logger
from rclpy.node import Node

from rclpy.qos import qos_profile_sensor_data

from std_msgs.msg import Int32
from std_msgs.msg import Int8
from std_msgs.msg import Bool

from utils.python_utils import printlog

from usr_msgs.msg import Planner as planner_msg
from usr_msgs.msg import LandMark
from usr_msgs.msg import Waypoint
from usr_msgs.msg import TurnRef
from usr_msgs.msg import Kiwibot

from usr_srvs.srv import Move
from usr_srvs.srv import Turn

# =============================================================================
def setProcessName(name: str) -> None:
    """!
    Function for seting the process name
    @see name 'str' defining the process name
    """
    if sys.platform in ["linux2", "linux"]:
        import ctypes

        libc = ctypes.cdll.LoadLibrary("libc.so.6")
        libc.prctl(15, name, 0, 0, 0)
    else:
        raise Exception(
            "Can not set the process name on non-linux systems: " + str(sys.platform)
        )


def read_yaml_file(CONF_PATH: str, FILE_NAME: str) -> dict:
    """!
    Function for seting the process name
    @param CONF_PATH `string` absolute path to configuration of cameras
    @param FILE_NAME `string` name of cameras configuration file
    @return data_loaded `dictionary` key: camera labels, values: dictionary with camera
            properties and settings, see yaml file for more details
    """

    abs_path = os.path.join(CONF_PATH, FILE_NAME)
    if os.path.isfile(abs_path):
        with open(abs_path, "r") as stream:
            data_loaded = yaml.safe_load(stream)
            return data_loaded
    else:
        return []


# =============================================================================
class PlannerNode(Node):
    def __init__(self) -> None:
        """
            Class constructor for path planning node
        Args:
        Returns:
        """

        # ---------------------------------------------------------------------
        Node.__init__(self, node_name="planner_node")

        # Allow callbacks to be executed in parallel without restriction.
        self.callback_group = ReentrantCallbackGroup()

        # ---------------------------------------------------------------------
        # Environment variables for forware and turn profiles
        self._TURN_ACELERATION_FC = float(os.getenv("TURN_ACELERATION_FC", default=0.3))
        self._TURN_CRTL_POINTS = int(os.getenv("TURN_CRTL_POINTS", default=30))
        self._FORWARE_ACELERATION_FC = float(
            os.getenv("FORWARE_ACELERATION_FC", default=0.3)
        )
        self._FORWARE_CRTL_POINTS = int(os.getenv("FORWARE_CRTL_POINTS", default=30))
        self._TURN_TIME = float(os.getenv("TURN_TIME", default=3.0))

        # ---------------------------------------------------------------------
        # Map features
        self.map_points = []  # Landmarks or keypoints in map
        self.map_duration = 0.0  # Map duration in [s]
        self.map_difficulty = 0.0  # Map difficulty [0.0-5.0]
        self.map_distance = 0.0  # Map distance in [m]
        self.way_points = {}  # List of waypoints in the path planning routine

        self._in_execution = False

        # Read routines from the yaml file in the configs folder
        self.routines = read_yaml_file(
            CONF_PATH="/workspace/planner/configs",
            FILE_NAME="routines.yaml",
        )

        # ---------------------------------------------------------------------
        # Subscribers

        self.sub_start_routine = self.create_subscription(
            msg_type=Int32,
            topic="/graphics/start_routine",
            callback=self.cb_start_routine,
            qos_profile=qos_profile_sensor_data,
            callback_group=self.callback_group,
        )

        self.kiwibot_state = Kiwibot()
        self.sub_kiwibot_stat = self.create_subscription(
            msg_type=Kiwibot,
            topic="/kiwibot/status",
            callback=self.cb_kiwibot_status,
            qos_profile=qos_profile_sensor_data,
            callback_group=self.callback_group,
        )

        self.sub_planner_pause = self.create_subscription(
            msg_type=Int32,
            topic="/graphics/pause",
            callback=self.cb_planner_pause,
            qos_profile=qos_profile_sensor_data,
            callback_group=self.callback_group,
        )

        self.is_paused = False

        # ---------------------------------------------------------------------
        # Publishers

        self.pub_path_planner = self.create_publisher(
            msg_type=planner_msg,
            topic="/path_planner/msg",
            qos_profile=qos_profile_sensor_data,
            callback_group=self.callback_group,
        )

        self.pub_speaker = self.create_publisher(
            msg_type=Int8,
            topic="/device/speaker/command",
            qos_profile=qos_profile_sensor_data,
            callback_group=self.callback_group,
        )

        # Publisher to send the order of start or stop recording video

        self.pub_recording = self.create_publisher(
            msg_type=Bool,
            topic="/path_planner/record",
            qos_profile=qos_profile_sensor_data,
            callback_group=self.callback_group,
        )

        # ---------------------------------------------------------------------
        # Services

        # service client to turn the robot
        self.cli_robot_turn = self.create_client(Turn, "/robot/turn")

        # service client to move the robot
        self.cli_robot_move = self.create_client(Move, "/robot/move")

        try:
            self.robot_turn_req = Turn.Request()
            self.robot_move_req = Move.Request()
        except Exception as e:
            printlog(
                msg="No services for robot actions, {}".format(e),
                msg_type="ERROR",
            )

    def cb_kiwibot_status(self, msg: Kiwibot) -> None:
        """
            Callback to update kiwibot state information in visuals
        Args:
            msg: `Kiwibot` message with  kiwibot state information
                int8 pos_x      # x axis position in the map
                int8 pos_y      # y axis position in the map
                float32 dist    # distance traveled by robot
                float32 speed   # speed m/s
                float32 time    # time since robot is moving
                float32 yaw     # time since robot is moving
                bool moving     # Robot is moving
        Returns:
        """

        try:
            self.kiwibot_state = msg

        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            printlog(
                msg="{}, {}, {}, {}".format(e, exc_type, fname, exc_tb.tb_lineno),
                msg_type="ERROR",
            )

    def cb_start_routine(self, msg: Int32) -> None:
        """
            Callback when a routine is started from visuals
        Args:
            msg: `Int32` number of routine to load waypoint from landmarks
        Returns:
        """

        try:

            if self._in_execution:
                printlog(msg="There's already a routine in execution", msg_type="WARN")
                return

            self._in_execution = True

            # Check that the routine in received exists in the routines list
            if msg.data in self.routines.keys():

                # -------------------------------------------------------
                # Read the waypoint or landmarks for the specified route
                self.way_points = self.read_keypoints(
                    land_marks_path="/workspace/planner/configs/key_points.csv",
                    key_Points=self.routines[msg.data],
                )

                # Publish routine for graphics components
                self.pub_path_planner.publish(
                    planner_msg(
                        land_marks=[
                            LandMark(
                                neighbors=[],
                                id=idx,
                                x=int(way_point_coord[0]),
                                y=int(way_point_coord[1]),
                            )
                            for idx, way_point_coord in enumerate(
                                self.way_points["coords"]
                            )
                        ],
                        distance=self.map_distance,
                        duration=self.map_duration,
                        difficulty=self.map_difficulty,
                    )
                )

                # Start video
                self.pub_recording.publish(Bool(data=True))

                # -------------------------------------------------------
                # Get the robot in the initial position
                printlog(
                    msg="setting the robot in origin",
                    msg_type="OKPURPLE",
                )
                self.robot_move_req.waypoints = [
                    Waypoint(
                        id=0,
                        x=int(self.way_points["coords"][0][0]),
                        y=int(self.way_points["coords"][0][1]),
                        t=0.0,
                        dt=0.0,
                    )
                ]
                move_resp = self.cli_robot_move.call(self.robot_move_req)

                # -------------------------------------------------------
                # Execute planning process
                self.pub_speaker.publish(Int8(data=2))
                for idx, way_point in enumerate(self.way_points["coords"][:-1]):

                    if self.is_paused:
                        # If the 'p' button is pressed, the next landmark will stop
                        self._in_execution = False
                        self.is_paused = False
                        break
                    else:

                        # -------------------------------------------------------
                        # Calculate the angle to turn the robot
                        dy = (
                            self.way_points["coords"][idx][1]
                            - self.way_points["coords"][idx + 1][1]
                        )
                        dx = (
                            self.way_points["coords"][idx + 1][0]
                            - self.way_points["coords"][idx][0]
                        )
                        ang = np.rad2deg(np.arctan2(dy, dx))
                        dang = ang - self.kiwibot_state.yaw

                        if abs(dang) > 180:
                            dang += 360
                        elif dang > 360:
                            dang -= 360
                        elif dang > 180:
                            dang -= 360
                        elif dang < -180:
                            dang += 360

                        if int(dang):

                            printlog(
                                msg=f"turning robot to reference {idx+1}",
                                msg_type="OKPURPLE",
                            )

                            # Generate the turning profile to get the robot aligned to the next landmark
                            self.robot_turn_req.turn_ref = [
                                TurnRef(
                                    id=turn_reference["idx"],
                                    yaw=turn_reference["a"],
                                    t=turn_reference["t"],
                                    dt=turn_reference["dt"],
                                )
                                for turn_reference in self.get_profile_turn(
                                    dst=dang,
                                    time=self._TURN_TIME,
                                    pt=self._TURN_ACELERATION_FC,
                                    n=self._TURN_CRTL_POINTS,
                                )
                            ]

                            move_resp = self.cli_robot_turn.call(self.robot_turn_req)

                        # -------------------------------------------------------
                        printlog(
                            msg=f"moving robot to landmark {idx}",
                            msg_type="OKPURPLE",
                        )

                        # Generate the waypoints to the next landmark
                        seg_way_points = self.get_profile_route(
                            src=self.way_points["coords"][idx],
                            dst=self.way_points["coords"][idx + 1],
                            time=self.way_points["times"][idx],
                            pt=self._FORWARE_ACELERATION_FC,
                            n=self._FORWARE_CRTL_POINTS,
                        )

                        # Move the robot to the next landmark
                        self.robot_move_req.waypoints = [
                            Waypoint(
                                id=wp["idx"],
                                x=int(wp["pt"][0]),
                                y=int(wp["pt"][1]),
                                t=wp["t"],
                                dt=wp["dt"],
                            )
                            for wp in seg_way_points
                        ]

                        move_resp = self.cli_robot_move.call(self.robot_move_req)

                # -------------------------------------------------------
                if not self._in_execution:
                    printlog(
                        msg=f"routine {msg.data} execution has been stopped",
                        msg_type="WARN",
                    )
                else:
                    printlog(
                        msg=f"routine {msg.data} has finished",
                        msg_type="OKGREEN",
                    )

                # -------------------------------------------------------
                self.pub_speaker.publish(Int8(data=3))

                # Stop video
                self.pub_recording.publish(Bool(data=False))

            else:
                printlog(
                    msg=f"routine {msg.data} does not exit",
                    msg_type="WARN",
                )
                return

        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            printlog(
                msg="{}, {}, {}, {}".format(e, exc_type, fname, exc_tb.tb_lineno),
                msg_type="ERROR",
            )

        self._in_execution = False

    def read_keypoints(self, land_marks_path: str, key_Points: list) -> list:
        """
            Reads and loads maps key points configuration and create way points
            for robots trajectory
        Args:
            land_marks_path: `string` absolute path to maps keypoints configuration path
            key_Points: `list` of tuples as (key_points, code of trajectory)
        Returns:
            way_points: `dict` coords: coordinates to follow and times: times for
                each coordinate
        """

        # Auxiliar variables
        way_points = {"coords": [], "times": [], "distances": []}
        map_difficulty = []

        # Check if file exits
        if not os.path.isfile(land_marks_path):
            print("[ERROR]: No configuration file")
            return way_points

        # Open and read csv file
        with open(land_marks_path, "r") as csv_file:
            csv_reader = csv.reader(csv_file)
            for idx, line in enumerate(csv_reader):
                if idx != 0:
                    self.map_points.append(
                        {
                            "src_id": int(line[0]),
                            "src_coord": (int(line[1]), int(line[2])),
                            "dst_id": int(line[3]),
                            "dst_coord": (int(line[4]), int(line[5])),
                            "difficulty": float(line[6]),
                            "code": int(line[7]),
                            "description": line[8],
                            "distance": float(line[9]),
                            "time": float(line[10]),
                        }
                    )

        # Generate map
        for idx, key_pt in enumerate(key_Points[:-1]):
            match_src = [
                dic
                for dic in self.map_points
                if dic["src_id"] == key_pt[0]
                and dic["dst_id"] == key_Points[idx + 1][0]
                and dic["code"] == key_pt[1]
            ]
            if len(match_src):
                self.map_duration += match_src[0]["time"]
                self.map_distance += match_src[0]["distance"] / 100
                map_difficulty.append(match_src[0]["difficulty"])

                if not len(way_points["coords"]):
                    way_points["coords"].append(match_src[-1]["src_coord"])
                way_points["coords"].append(match_src[-1]["dst_coord"])
                way_points["times"].append(match_src[-1]["time"])
                way_points["distances"].append(match_src[-1]["distance"] / 100)

            else:
                print(
                    "[ERROR]: THERE'S NO A DEFINED TRAJECTORY FROM {} TO {}".format(
                        key_pt[0], key_Points[idx + 1][0]
                    )
                )
                break

        # Get maps difficulty
        self.map_difficulty = round(
            np.mean(map_difficulty) if len(map_difficulty) else None, 2
        )
        self.map_distance = round(self.map_distance, 2)

        return way_points

    def get_profile_route(
        self, src: tuple, dst: tuple, time: float, pt=0.3, n=30
    ) -> list:
        """
            Generates waypoints: coordinates and times with a trapezoidal profile
        Args:
            src: `tuple` origin coordinate (X, Y)
            dst: `tuple` destination coordinate (X, Y)
            time: `float` time from origin to destination
            pt: `float` deceleration/acceleration factor
            n: `int` control points to discrite the trajectory
        Returns:
            way_points: `dict` coordinates and times of trajectory with trapezoidal profile
                every element in the list is  dictionary with the keys:
                {
                    "idx": [int](index of the waypoint),
                    "pt": [tuple][int](x and y axis positions in the image space),
                    "t": [float](time for angle a),
                    "dt": [float](sept of time for angle a, is constant element)
                }
        """

        way_points = []

        # ---------------------------------------------------------------------
        # TODO: Trapezoidal speed profile
        # Add your solution here, remeber that every element in the list is a dictionary
        # where every element in has the next structure and data type:
        # "idx": [int](index of the waypoint),
        # "pt": [tuple][int](x and y axis positions in the image space),
        # "t": [float](time for angle a),
        # "dt": [float](sept of time for angle a, is constant element)
        # Do not forget and respect the keys names

        # Calculate the step of time for every waypoint
        dt = time / n

        # Define the discretized vector of time, from 0 to time, with dt step
        t_disc = []
        prev = 0.0
        for i in range(0,n):
            t_disc.append(prev+dt)
            prev += dt

        # On each axis, calculate distance and v_max for trapezoidal profile
        d_x = dst[0] - src[0]
        d_y = dst[1] - src[1]
        v_max_x = d_x / ( time * ( 1.0 - pt ) )
        v_max_y = d_y / ( time * ( 1.0 - pt ) )

        # For each stage of the trapezoidal profile (acceleration, constant speed, 
        # and deacceleration), calculate the velocity and position on each axis.
        vel_x = [0.0]*len(t_disc)
        vel_y = [0.0]*len(t_disc)
        pos_x = [src[0]]*len(t_disc)
        pos_y = [src[1]]*len(t_disc)

        for index, t_val in enumerate(t_disc):
            if index > 0:
                if t_val <= time * pt:
                    # Acceleration
                    vel_x[index] = (v_max_x * t_val) / (pt * time)
                    vel_y[index] = (v_max_y * t_val) / (pt * time)
                    pos_x[index] = pos_x[index-1] + 1/2 * dt * (vel_x[index] + vel_x[index - 1])
                    pos_y[index] = pos_y[index-1] + 1/2 * dt * (vel_y[index] + vel_y[index - 1])
                elif t_val > time * pt and t_val <= (time * (1 - pt)):
                    # Constant speed
                    vel_x[index] = v_max_x
                    vel_y[index] = v_max_y
                    pos_x[index] = pos_x[index - 1] + dt * v_max_x
                    pos_y[index] = pos_y[index - 1] + dt * v_max_y
                elif t_val > (time * (1 - pt)):
                    # Deacceleration
                    vel_x[index] = v_max_x - ((v_max_x*(t_val - (time * (1 - pt))))/(pt * time))
                    vel_y[index] = v_max_y - ((v_max_y*(t_val - (time * (1 - pt))))/(pt * time))
                    pos_x[index] = pos_x[index - 1] + 1/2 * dt * (vel_x[index] + vel_x[index - 1])
                    pos_y[index] = pos_y[index - 1] + 1/2 * dt * (vel_y[index] + vel_y[index - 1])

        # Fill the way_points dictionary
        for index, t_val in enumerate(t_disc):
            way_points.append(
                {
                    "idx": index,
                    "pt": (int(round(pos_x[index])), int(round(pos_y[index]))),
                    "t": t_val,
                    "dt": dt,
                }
            )
        
        # ---------------------------------------------------------------------

        return way_points

    def get_profile_turn(self, dst: float, time: float, pt=0.3, n=30) -> list:
        """
            Generates waypoints: coordinates and times with a trapezoidal turning profile
        Args:
            dst: `float` target angle
            time: `float` time for turning angle
            pt: `float` deceleration/acceleration factor
            n: `int` control points to discrite the trajectory
        Returns:
            turn_points: `dict` coordinates and times of turn with trapezoidal profile
                every element in the list is  dictionary with the keys:
                {
                    "idx": [int](index of the waypoint),
                    "a": [float](yaw angle of the robot),
                    "t": [float](time for angle a),
                    "dt": [float](sept of time for angle a, is constant element)
                }
        """

        turn_points = []
        if dst == 0.0:
            return turn_points

        # ---------------------------------------------------------------------
        # TODO: Trapezoidal turn profile
        # Add your solution here, remeber that every element in the list is a dictionary
        # where every element in has the next structure and data type:
        # "idx": [int](index of the waypoint),
        # "a": [float](yaw angle of the robot),
        # "t": [float](time for angle a),
        # "dt": [float](sept of time for angle a, is constant element)
        # Do not forget and respect the keys names

        # Calculate the step of time for every waypoint
        dt = time / n

        # Define the discretized vector of time, from 0 to time, with dt step
        t_disc = []
        prev = 0.0
        for i in range(0,n):
            t_disc.append(prev+dt)
            prev += dt

        # Calculate max angular velocity w_max for trapezoidal profile
        w_max = dst / ( time * ( 1.0 - pt ) )

        # For each stage of the trapezoidal profile (acceleration, constant speed, 
        # and deacceleration), calculate the velocity and position on rotation.
        w_a = [0.0]*len(t_disc)
        th_a = [0.0]*len(t_disc)
        for index, t_val in enumerate(t_disc):
            if index > 0:
                if t_val <= time * pt:
                    # Acceleration
                    w_a[index] = (w_max * t_val) / (pt * time)
                    th_a[index] = th_a[index-1] + 1/2 * dt * (w_a[index] + w_a[index - 1])
                elif t_val > time * pt and t_val <= (time * (1 - pt)):
                    # Constant speed
                    w_a[index] = w_max
                    th_a[index] = th_a[index - 1] + dt * w_max
                elif t_val > (time * (1 - pt)):
                    # Deacceleration
                    w_a[index] = w_max - ((w_max*(t_val - (time * (1 - pt))))/(pt * time))
                    th_a[index] = th_a[index - 1] + 1/2 * dt * (w_a[index] + w_a[index - 1])

        # Fill the turn_points dictionary
        for index, t_val in enumerate(t_disc):
            turn_points.append(
                {
                    "idx": index,
                    "a": round(th_a[index],1),
                    "t": t_val,
                    "dt": dt,
                }
            )
        # ---------------------------------------------------------------------
        return turn_points

    def cb_planner_pause(self, msg: Int32) -> None:
        """
            Callback to stop a routine
        Args:
            msg: `Int32` stop command
        Returns:
        """
        if self.is_paused:
            self.is_paused = True
        else:
            self.is_paused = True


# =============================================================================
def main(args=None) -> None:
    """!
    Main Functions of Local Console Node
    """
    # Initialize ROS communications for a given context.
    setProcessName("planner-node")
    rclpy.init(args=args)

    # Execute work and block until the context associated with the
    # executor is shutdown.
    planner_node = PlannerNode()

    # Runs callbacks in a pool of threads.
    executor = MultiThreadedExecutor()

    # Execute work and block until the context associated with the
    # executor is shutdown. Callbacks will be executed by the provided
    # executor.
    rclpy.spin(planner_node, executor)

    # Clear thread
    planner_node.clear()

    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    planner_node.destroy_node()
    rclpy.shutdown()


# =============================================================================
if __name__ == "__main__":
    main()

# =============================================================================
