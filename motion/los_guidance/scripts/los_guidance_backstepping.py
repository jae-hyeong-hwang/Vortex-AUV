#!/usr/bin/env python
# Written by Kristoffer Rakstad Solberg, Student
# Documented by Christopher Strom and Jae Hyeong Hwang
# Copyright (c) 2020 Manta AUV, Vortex NTNU.
# All rights reserved.

import rospy
import numpy as np
import math
from vortex_msgs.msg import PropulsionCommand
from nav_msgs.msg import Odometry
from geometry_msgs.msg import Wrench, PoseStamped, Pose
from tf.transformations import euler_from_quaternion, quaternion_from_euler

# dynamic reconfigure
from dynamic_reconfigure.server import Server
from los_guidance.cfg import AutopilotConfig

# action message
import actionlib
from vortex_msgs.msg import LosPathFollowingAction, LosPathFollowingGoal, LosPathFollowingResult, LosPathFollowingFeedback

# modules included in this package
from autopilot.autopilot import AutopilotBackstepping, AutopilotPID
from reference_model.discrete_tustin import ReferenceModel

class LOS:
	"""
	The Line-Of-Sight guidance class, with an imported controller.

	Physical attributes referenced in the class:
	x, y, z: 	surge, sway, heave (position)
	u, v, w:	surge, sway, heave (velocity)

	alpha:	The path-tangential angle
  	psi:	Heading angle required to reach the LOS intersection
	  		point.

	R: sphere of acceptance. If the AUV is inside the sphere
	   defined by this radius and the setpoint, it will be
	   considered to have reached the setpoint.
	"""

	def __init__(self):

		# update rate
		self.h = 0.05
		self.u = 0.0

		# current position
		self.x = 0.0
		self.y = 0.0
		self.z = 0.0

		# previous waypoint
		self.x_k = 0.0
		self.y_k = 0.0

		# next waypoint
		self.x_kp1 = 0.0
		self.y_kp1 = 0.0

		# depth hold depth
		self.z_d = 0.0

		# desired speed
		self.speed = 0

		# sphere of acceptance
		self.R = 0.5

		# look-ahead distance
		Lpp = 0.7
		self.delta = 1.0*Lpp


	def updateState(self, x, y, z, u, v, w, psi, r, time):
		"""
		Update all state values contained in the LOS class.

		Args:
			x	  Surge; position in the direction of the x-axis.
			y	  Sway;  position in the direction of the y-axis.
			z	  Heave; position in the direction of the z-axis.

			u	  Body fixed velocity in the x-direction.
			v	  Body fixed velocity in the y-direction.
			w	  Body fixed velocity in the z-direction.

  			psi	  Heading angle required to reach the LOS intersection
			  	  point.
			r
			time  A double with the current time
		"""

		# Update position
		self.x = x
		self.y = y
		self.z = z

		# Update velocities
		self.u_dot = (u - self.u) / self.h
		self.u = u
		self.v = v
		self.w = w
		
		self.psi = psi
		self.r = r
		self.t = time

	def setWayPoints(self, x_k, y_k, x_kp1, y_kp1):
		"""
		Set the previous and next waypoints

		Args:
			x_k     x-component of the previous waypoint
			y_k     y-component of the previous waypoint

			x_kp1	x-component of the next waypoint
			y_kp1	y-component of the next waypoint
		"""

		# previous waypoint
		self.x_k = x_k
		self.y_k = y_k

		# next waypoint
		self.x_kp1 = x_kp1
		self.y_kp1 = y_kp1

	def distance(self):
		"""
		Calculate straight line distance (2D) between the
		current position and the setpoint position.
		"""

		return np.sqrt((self.x_kp1 - self.x)**2 + 
					   (self.y_kp1 - self.y)**2 )


	def sphereOfAcceptance(self):
		"""
		The sphere of acceptance is a sphere around the setpoint.
		If the AUV is inside this sphere, it will be considered
		as having reached the setpoint.

		Returns:
			bool:	True if the current position is less than the
					radius of the sphere of acceptance. False otherwise
		"""

		return self.distance() < self.R


	def getEpsilonVector(self):
		"""
		Calculate the epsilon vector, which is the vector
		that contains the coordinates of the AUV in the 
		path-fixed reference frame for a straight line going
		from the reference point to the target position.

		Returns:
			float: The calculated epsilon vector

		"""

		alpha = self.alpha

		# rotation matrix
		R = np.array(( (np.cos(alpha), -np.sin(alpha)),
					   (np.sin(alpha),  np.cos(alpha)) ))

		# transpose
		R_T = np.transpose(R)

		# position vector
		p_t = np.array((self.x, self.y))
		p_k = np.array((self.x_k, self.y_k))

		# epsilon (eq 10.56 Fossen)
		epsilon = R_T.dot(p_t - p_k) 

		return epsilon

	def quat2euler(self,msg):
		"""
		Calculate roll, pitch and yaw from the orientation
		quaternion with the axis sequence xyzw

		Args:
			msg		A nav_msgs/Odometry message

		Returns:
			float: The euler yaw angle calculated from the msg argument
		"""

		global roll, pitch, yaw
		orientation_q = msg.pose.pose.orientation
		orientation_list = [orientation_q.x, orientation_q.y, orientation_q.z, orientation_q.w]
		(roll,pitch,yaw) = euler_from_quaternion(orientation_list)

		return yaw

	def lookaheadBasedSteering(self):
		"""
		Calculate the desired heading angle. This angle is
		the sum of the path-tangential angle and the velocity-
		path relative angle.

		Returns:
			float: The desired heading angle chi_d
		"""

		# straight-line path segment
		self.y_delta = self.y_kp1 - self.y_k
		self.x_delta = self.x_kp1 - self.x_k

		# angle
		self.alpha = np.arctan2(self.y_delta, self.x_delta) 

		# rotation matrix
		epsilon = self.getEpsilonVector()

		# along track distance
		self.s = epsilon[0]

		# cross-track error
		self.e = epsilon[1]

		# path-tangential angle (eq 10.73 Fossen)
		self.chi_p = self.alpha

		# velocity-path relative angle (eq 10.74 Fossen)
		self.chi_r = np.arctan(-self.e / self.delta)

		# desired heading angle
		self.chi_d = self.chi_p + self.chi_r

		return self.chi_d

class LosPathFollowing(object):
	"""
	This is the ROS wrapper class for the LOS class. 

	Attributes:
		_feedback	A vortex_msgs action that contains the distance to goal
		_result		A vortex_msgs action, true if a goal is set within the
					sphereof acceptance, false if not
	
	Nodes created:
		los_path_following

	Subscribes to:
		/odometry/filtered
	
	Publishes to:
		/manta/thruster_manager/input
		/manta/los_desired
	
	"""

	# create messages that are used to send feedback/result
	_feedback = LosPathFollowingFeedback()
	_result = LosPathFollowingResult()

	def __init__(self):
		"""
		To initialize the ROS wrapper, the node, subscribers
		and publishers are set up. The high-level guidance and
		controller objects are also intialized. Lastly, dynamic
		reconfigure and action servers are set up.
		"""

		"""
		A flag to indicate whether or not a goal has not been reached.
		True means that a goal is in progress of being completed.
		False means that a goal has been completed (or not started
		with any goal)
		"""
		self.flag = False

		rospy.init_node('los_path_following')
		self.sub = rospy.Subscriber('/odometry/filtered', Odometry, self.callback, queue_size=1) # 20hz
		self.pub_thrust = rospy.Publisher('/manta/thruster_manager/input', Wrench, queue_size=1)
		self.pub_desired = rospy.Publisher('/manta/los_desired', Odometry, queue_size=1)

		# constructor object
		self.los = LOS()
		self.PID = AutopilotPID()
		self.autopilot = AutopilotBackstepping()
		self.reference_model = ReferenceModel(np.array((0, 0)), self.los.h)

		# dynamic reconfigure
		self.config = {}
		self.srv_reconfigure = Server(AutopilotConfig, self.config_callback)

		"""
			action server guide
			https://github.com/strawlab/ros_common/blob/master/actionlib/src/actionlib/simple_action_server.py
		"""
		self.action_server = actionlib.SimpleActionServer(name='los_path', ActionSpec=LosPathFollowingAction, auto_start=False)
		self.action_server.register_goal_callback(self.goalCB)
		self.action_server.register_preempt_callback(self.preemptCB)
		self.action_server.start()

	def fixHeadingWrapping(self):
		"""
		The heading angle is obtained by the use of an arctangent
		function, which is discontinuous at -pi and pi. This can 
		be problematic when the heading angle is fed into the
		reference model. This function fixes this problem by
		wrapping the angles around by 2pi.
		"""

		e = self.psi - self.psi_ref
		if e < -math.pi:
			self.psi_ref = self.psi_ref - 2*math.pi
		if e > math.pi:
			self.psi_ref = self.psi_ref + 2*math.pi


		# reference model
		x_d = self.reference_model.discreteTustinMSD(np.array((self.los.speed, self.psi_ref)))
		psi_d = x_d[2]

		e = self.psi - psi_d
		if e > math.pi:
			psi_d = psi_d - 2*math.pi
			self.reference_model = ReferenceModel(np.array((self.los.u, self.los.psi)), self.los.h)
			x_d = self.reference_model.discreteTustinMSD(np.array((self.los.speed, psi_d)))
		if e < -math.pi:
			psi_d = psi_d + 2*math.pi
			self.reference_model = ReferenceModel(np.array((self.los.u, self.los.psi)), self.los.h)
			x_d = self.reference_model.discreteTustinMSD(np.array((self.los.speed, psi_d)))

		return x_d


	def callback(self, msg): 
		"""
		The callback used in the subscribed topic /odometry/filtered.
		When called, position and velocity states are updated, and 
		a new current goal is set.

		If the flag attribute is True, we have not yet reached a goal
		and so a control force is published, alongside the desired
		pose.

		Args:
			msg		A nav_msgs/Odometry ROS message type
		"""

		# update current position
		self.psi = self.los.quat2euler(msg)

		# update position and velocities
		self.los.updateState(msg.pose.pose.position.x, msg.pose.pose.position.y, msg.pose.pose.position.z,
							 msg.twist.twist.linear.x, msg.twist.twist.linear.y, msg.twist.twist.linear.z,
							 self.psi, msg.twist.twist.angular.z, msg.header.stamp.to_sec())

		# update current goal
		self.psi_ref = self.los.lookaheadBasedSteering()


		if self.flag is True:

			"""
				Wrapping would have been avoided by using quaternions instead of Euler angles
				if you don't care about wrapping, use this instead:

				x_d = self.reference_model.discreteTustinMSD(np.array((self.los.speed,psi_d)))
			"""
			x_d = self.fixHeadingWrapping()

			u_d = x_d[0]
			u_d_dot = x_d[1]
			psi_d = x_d[2]
			r_d = x_d[3]
			r_d_dot = x_d[4]

			los_msg = Odometry()
			los_msg.header.stamp = rospy.Time.now()
			quat_d = quaternion_from_euler(0, 0, psi_d)
			los_msg.pose.pose.position.z = msg.pose.pose.position.z
			los_msg.pose.pose.orientation.x = quat_d[0]
			los_msg.pose.pose.orientation.y = quat_d[1]
			los_msg.pose.pose.orientation.z = quat_d[2]
			los_msg.pose.pose.orientation.w = quat_d[3]
			los_msg.twist.twist.linear.x = u_d
			los_msg.twist.twist.angular.z = r_d
			self.pub_desired.publish(los_msg)

			# control force
			tau_d = self.autopilot.backstepping.controlLaw(self.los.u, self.los.u_dot, u_d, u_d_dot, self.los.v, self.psi, psi_d, self.los.r, r_d, r_d_dot)
			tau_depth_hold = self.PID.depthController(self.los.z_d, self.los.z, self.los.t)

			# add speed controllers here
			thrust_msg = Wrench()
			if tau_d[0] > 0.0:
				thrust_msg.force.x = tau_d[0]

			thrust_msg.force.y = tau_d[1]
			thrust_msg.force.z = tau_depth_hold
			thrust_msg.torque.z = tau_d[2] # 2.0*self.error_ENU

			# write to thrusters
			self.pub_thrust.publish(thrust_msg)


			# check if action goal succeeded
			self.statusActionGoal()

	def statusActionGoal(self):
		"""
		Publish the current distance to target and check if the current
		position is within the sphere of acceptance. If it is, the 
		attribute _result can be set to true, and the flag to false.
		"""

		# feedback
		self._feedback.distanceToGoal = self.los.distance()
		self.action_server.publish_feedback(self._feedback)

		# succeeded
		if self.los.sphereOfAcceptance():
			self._result.terminalSector = True
			self.action_server.set_succeeded(self._result, text="goal completed")
			self.flag = False

	def preemptCB(self):
		"""
		The preempt callback for the action server.
		"""

		# check that preempt has not been requested by the client
		if self.action_server.is_preempt_requested():
			rospy.loginfo("Preempted requested by los path client")
			self.action_server.set_preempted()
			self.flag = False

	def goalCB(self):
		"""
		The goal callback for the action server.
		"""

		self.flag = True
		_goal = self.action_server.accept_new_goal()

		# set goal
		self.los.x_k = self.los.x
		self.los.y_k = self.los.y
		self.los.x_kp1 = _goal.next_waypoint.x
		self.los.y_kp1 = _goal.next_waypoint.y

		# forward speed
		self.los.speed = _goal.forward_speed.linear.x

		# depth hold
		self.los.z_d = _goal.desired_depth.z

		# sphere of acceptance
		self.los.R = _goal.sphereOfAcceptance

		self.reference_model = ReferenceModel(np.array((self.los.u, self.los.psi)), self.los.h)


	def config_callback(self, config, level):
		"""
		Handle updated configuration values.
		
		Args:
			config	The dynamic reconfigure server's config
			level	Ununsed variable

		Returns:
			The updated config argument.
		"""

		# Config has changed, reset PID controllers
		rospy.loginfo("""Reconfigure Request: {delta}, {p_rot}, {i_rot}, {d_rot}, {sat_rot} """.format(**config))
        
		# update look-ahead distance
		self.los.delta = config['delta']

		# self.pid_lin = PIDRegulator(config['pos_p'], config['pos_i'], config['pos_d'], config['pos_sat'])
		self.autopilot.updateGains(config['p_rot'], config['i_rot'], config['d_rot'], config['sat_rot'])

		# update config
		self.config = config

		return config


if __name__ == '__main__':
	try:
		los_path_following = LosPathFollowing()
		rospy.spin()
	except rospy.ROSInterruptException:
		pass