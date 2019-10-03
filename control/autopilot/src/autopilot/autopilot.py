from PID import PIDRegulator

class Autopilot:

	def __init__(self):
		# PIDRegulator(p, i, d, sat)
		self.pid_heading = PIDRegulator(5, 1.0, 0.0, 1.0)

	def updateGains(self, p, i, d, sat):

		self.pid_heading.p = p
		self.pid_heading.i = i
		self.pid_heading.d = d
		self.pid_heading.sat = sat

	def headingController(self, psi_d, psi, t):

		# error ENU
		e_rot = psi_d - psi

		# regulate(err, t)
		tau = self.pid_heading.regulate(e_rot, t)

		return tau