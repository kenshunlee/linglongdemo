#!/usr/bin/env python3
# coding=utf-8
# Linglong-H SDK extension (sdk_extend.py)
"""继承 maniSdkClass：增加与 udp_bridge 一致的后台 sens 交换线程。"""
import json
import csv
import socket
import struct
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
import numpy as np

from .sdk_base import (
	DEFAULT_CMD_PORT,
	DEFAULT_MODE_PORT,
	DEFAULT_ROBOT_IP,
	DEFAULT_STATE_PORT,
	ManiInterpStartSource,
	RobotModeManager,
	maniSdkClass,
	maniSdkSensDataClass,
)

DEFAULT_NAV_IP = "192.168.1.204"
DEFAULT_NAV_PORT = 19206
DEFAULT_NAV_MSG_TYPE = 3051
# 独立目标 UDP（ObjectUdpReceiver）在未显式指定时的默认监听端口；发送端与此一致。
DEFAULT_OBJECT_UDP_LISTEN_PORT = 5005


def _interp_start_mode(interp_start: ManiInterpStartSource | str | int) -> str:
	"""返回 'ctrl' 或 'status'；接受 ``ManiInterpStartSource``、``'ctrl'/'status'`` 或 ``0/1``。"""
	if isinstance(interp_start, ManiInterpStartSource):
		return "ctrl" if interp_start == ManiInterpStartSource.kCtrl else "status"
	if isinstance(interp_start, str):
		v = interp_start.lower().strip()
		if v in ("ctrl", "control", "cmd"):
			return "ctrl"
		if v in ("status", "sens", "feedback", "state"):
			return "status"
		raise ValueError(f"interp_start 非法: {interp_start!r}，须为 ManiInterpStartSource、'ctrl' 或 'status'")
	if type(interp_start) is int:
		return "ctrl" if interp_start == 1 else "status"
	raise TypeError(f"interp_start 类型不支持: {type(interp_start)!r}")


def _pose_to_rotation_matrix_rpy(rpy: np.ndarray) -> np.ndarray:
	"""R = Rz(yaw) Ry(pitch) Rx(roll)，与 C++ ``pose_to_rotation_matrix`` 一致。"""
	roll, pitch, yaw = float(rpy[0]), float(rpy[1]), float(rpy[2])
	cr, sr = np.cos(roll), np.sin(roll)
	cp, sp = np.cos(pitch), np.sin(pitch)
	cy, sy = np.cos(yaw), np.sin(yaw)
	rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]], dtype=np.float64)
	ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]], dtype=np.float64)
	rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
	return rz @ ry @ rx


def _rotation_to_pose_rpy(m: np.ndarray) -> np.ndarray:
	"""与 C++ ``rotation_to_pose`` 一致。"""
	sin_pitch = -float(m[2, 0])
	pitch = float(np.arcsin(np.clip(sin_pitch, -1.0, 1.0)))
	if abs(m[2, 0]) < 0.999999:
		roll = float(np.arctan2(m[2, 1], m[2, 2]))
		yaw = float(np.arctan2(m[1, 0], m[0, 0]))
	else:
		roll = 0.0
		yaw = float(np.arctan2(-m[0, 1], m[1, 1]))
	return np.array([roll, pitch, yaw], dtype=np.float64)


@dataclass
class BaseToCameraExtrinsic:
	"""base_to_camera 语义：**相机系在基座系下的相对位姿** ``^base T_cam``。

	- ``translation``：相机原点在 **基座** 下的位置。
	- ``rpy_rad``：`R_z R_y R_x`，构造 ``R_base_cam``，满足 ``v_base = R_base_cam @ v_cam``。
	UDP 物体在相机下；输出物体在基座下：``p_base = R @ p_cam + translation``，``R_bo = R @ R_co``。
	"""
	translation: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
	rpy_rad: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])


def default_object_udp_base_to_camera_extrinsic() -> BaseToCameraExtrinsic:
	"""内置 ``^base T_cam``：`translation`、`rpy_rad` 与 C++ ``ObjectUdpReceiver`` 默认一致。"""
	return BaseToCameraExtrinsic(
		translation=[0.1, 0.0, 1.0],
		rpy_rad=[-2.28, 0.0, -1.5708],
	)


def apply_camera_to_base_extrinsic(
	ex: BaseToCameraExtrinsic,
	camera_pos,
	camera_att,
) -> tuple[list[float], list[float]]:
	"""``^base T_cam``（``ex``）× UDP 物体在相机下 → 物体在基座下的位置与 RPY。"""
	cp = np.asarray(camera_pos, dtype=np.float64).reshape(3)
	ca = np.asarray(camera_att, dtype=np.float64).reshape(3)
	t_b = np.asarray(ex.translation, dtype=np.float64).reshape(3)
	R_base_cam = _pose_to_rotation_matrix_rpy(np.asarray(ex.rpy_rad, dtype=np.float64).reshape(3))
	base_pos = (R_base_cam @ cp + t_b).astype(np.float64)
	R_co = _pose_to_rotation_matrix_rpy(ca)
	R_bo = R_base_cam @ R_co
	base_att = _rotation_to_pose_rpy(R_bo)
	return [float(base_pos[0]), float(base_pos[1]), float(base_pos[2])], [
		float(base_att[0]),
		float(base_att[1]),
		float(base_att[2]),
	]


@dataclass
class TrackedObject:
	"""目标信息：名称、相机/基座位姿 [x,y,z] / RPY（弧度，与机械臂 eef 约定一致）。"""

	name: str
	camera_pos: list[float]
	camera_att: list[float]
	base_pos: list[float]
	base_att: list[float]
	raw: dict | None = None  # 原始 JSON 子对象，便于上层取扩展字段


class ObjectUdpReceiver:
	"""独立的目标 UDP JSON 接收类。

	**职责**：对每个合法 UDP 包，把其中**全部**格式正确的对象按 ``name`` 记入内部表；
	不包含“该用哪一个物体”的决策——那叫由轨迹编排（例如 ``traj_replan``）按任务名等在
	``get_tracked_objects()`` 里**自行查 key**。

	仅接受根结构 ``{"objects": [ {...}, ... ]}``；每个元素须含字符串 ``name`` 以及
	``camera_pos``、``camera_att`` 各三维数组。其它形状整包忽略。
	``base_pos`` / ``base_att`` 由内建 ``^base T_cam``（``BaseToCameraExtrinsic``，见 ``default_object_udp_base_to_camera_extrinsic()``）与 UDP ``camera_*`` 组合得到物体在 **基座** 下的位姿。
	通常随 ``LinglongHSdkClass`` 建连启动；单测或自定义端口时可独立构造本类。
	"""

	def __init__(
		self,
		listen_port: int = DEFAULT_OBJECT_UDP_LISTEN_PORT,
		listen_ip: str = "0.0.0.0",
		timeout: float = 0.2,
	):
		"""初始化对象并设置默认成员。"""
		self._stop = threading.Event()  # 接收线程停止信号
		self._thread = None  # 接收线程句柄
		self._sock = None  # UDP socket
		self._lock = threading.Lock()  # 目标字典与外参读写锁
		self._tracked_objects: dict[str, TrackedObject] = {}  # 最新目标快照（key=目标名）
		self._base_to_camera_extrinsic = default_object_udp_base_to_camera_extrinsic()
		self.start(listen_port=listen_port, listen_ip=listen_ip, timeout=timeout)

	@staticmethod
	def _to_xyz3(value):
		"""内部辅助函数。"""
		if value is None:
			return None
		try:
			arr = np.asarray(value, dtype=float).reshape(-1)
		except Exception:
			return None
		if arr.size < 3:
			return None
		return [float(arr[0]), float(arr[1]), float(arr[2])]

	def _extract_tracked_objects(self, payload):
		"""仅解析 ``{"objects": [ {...}, ... ]}``；元素须含 name、camera_pos、camera_att。"""
		if not isinstance(payload, dict):
			return []
		if "objects" not in payload or not isinstance(payload["objects"], list):
			return []
		out = []
		for item in payload["objects"]:
			if not isinstance(item, dict):
				continue
			name = item.get("name")
			if not isinstance(name, str) or not name:
				continue
			camera_pos = self._to_xyz3(item.get("camera_pos"))
			camera_att = self._to_xyz3(item.get("camera_att"))
			if camera_pos is None or camera_att is None:
				continue
			with self._lock:
				ex = BaseToCameraExtrinsic(
					translation=list(self._base_to_camera_extrinsic.translation[:3]),
					rpy_rad=list(self._base_to_camera_extrinsic.rpy_rad[:3]),
				)
			base_pos, base_att = apply_camera_to_base_extrinsic(ex, camera_pos, camera_att)
			out.append(
				TrackedObject(
					name=name,
					camera_pos=camera_pos,
					camera_att=camera_att,
					base_pos=base_pos,
					base_att=base_att,
					raw=item,
				)
			)
		return out

	def _udp_loop(self):
		"""目标UDP接收线程主循环。"""
		while not self._stop.is_set():
			sk = self._sock
			if sk is None:
				break
			try:
				buf, _ = sk.recvfrom(65535)
			except socket.timeout:
				continue
			except OSError:
				break

			try:
				payload = json.loads(buf.decode("utf-8"))
			except Exception:
				continue

			objects = self._extract_tracked_objects(payload)
			if not objects:
				continue
			with self._lock:
				for obj in objects:
					self._tracked_objects[obj.name] = obj

	def start(
		self,
		listen_port: int = DEFAULT_OBJECT_UDP_LISTEN_PORT,
		listen_ip: str = "0.0.0.0",
		timeout: float = 0.2,
	):
		"""启动目标 JSON 接收线程。每包须为 ``{"objects":[...]}``。"""
		self.stop()
		with self._lock:
			self._base_to_camera_extrinsic = default_object_udp_base_to_camera_extrinsic()
		sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		sk.bind((listen_ip, int(listen_port)))
		sk.settimeout(float(timeout))
		self._sock = sk
		self._stop.clear()
		self._thread = threading.Thread(
			target=self._udp_loop,
			name="LinglongHSdk-object-udp",
			daemon=True,
		)
		self._thread.start()

	def stop(self):
		"""停止目标JSON接收线程并关闭socket。"""
		self._stop.set()
		t = self._thread
		if t is not None and t.is_alive():
			t.join(timeout=1.0)
		self._thread = None
		sk = self._sock
		self._sock = None
		if sk is not None:
			try:
				sk.close()
			except OSError:
				pass

	def get_tracked_objects(self):
		"""获取当前记录目标快照: {name: TrackedObject}。"""
		with self._lock:
			return dict(self._tracked_objects)


class LinglongHSdkClass(maniSdkClass):
	"""maniSdkClass + 灵龙后台线程：循环「发 sens + 收 sens」写 self.sens。"""

	CSV_SIGNAL_LENGTHS = {
		"arm_q_l": 7,
		"arm_q_r": 7,
		"waist_q": 4,
		"head_q": 2,
		"cap": 2,
		"base_vel": 2,
		"ee_pose_l": 6,
		"ee_pose_r": 6,
		"ee_pose_waist": 6,
	}

	def __init__(
		self,
		ip: str = DEFAULT_ROBOT_IP,
		port: int = DEFAULT_CMD_PORT,
		*,
		state_port: int = DEFAULT_STATE_PORT,
		chassis_tcp_on_send: bool = True,
		auto_state_thread: bool = True,
		state_poll_timeout: float = 0.02,
		debug: bool = False,
		auto_object_udp_thread: bool = True,
		object_udp_listen_port: int = DEFAULT_OBJECT_UDP_LISTEN_PORT,
		object_udp_listen_ip: str = "0.0.0.0",
		object_udp_timeout: float = 0.2,
	):
		"""初始化对象并设置默认成员。"""
		super().__init__(
			ip,
			port,
			state_port=state_port,
			chassis_tcp_on_send=chassis_tcp_on_send,
		)
		# 扩展层线程：状态轮询线程 + 调试打印线程。
		self._state_stop = threading.Event()  # 后台状态线程停止信号
		self._state_poll_timeout = float(state_poll_timeout)  # 后台状态线程单轮超时
		self._state_thread = None  # 后台状态线程句柄
		self._debug = bool(debug)  # 是否启用10Hz调试打印
		self._debug_stop = threading.Event()  # 调试线程停止信号
		self._debug_thread = None  # 调试线程句柄
		if auto_state_thread:
			self._state_thread = threading.Thread(
				target=self._state_thread_loop,
				name="LinglongHSdk-state",
				daemon=True,
			)
			self._state_thread.start()
		if self._debug:
			self._debug_thread = threading.Thread(
				target=self._debug_thread_loop,
				name="LinglongHSdk-debug",
				daemon=True,
			)
			self._debug_thread.start()
		# 导航链路参数（TCP: 19206）
		self.nav_target: tuple[str, int] = (DEFAULT_NAV_IP, DEFAULT_NAV_PORT)  # 导航TCP目标地址
		self.nav_timeout_s = 5.0  # 导航TCP超时（秒）
		self.nav_msg_type = DEFAULT_NAV_MSG_TYPE  # 导航消息类型（默认3051）
		self.nav_req_id = 1  # 导航请求序号（循环递增）

		self._object_udp: ObjectUdpReceiver | None = None
		if auto_object_udp_thread and int(object_udp_listen_port) > 0:
			self._object_udp = ObjectUdpReceiver(
				listen_port=int(object_udp_listen_port),
				listen_ip=str(object_udp_listen_ip),
				timeout=float(object_udp_timeout),
			)

	def object_udp_receiver(self) -> ObjectUdpReceiver | None:
		"""与 C++ ``LinglongHSdkClass::object_udp_receiver()`` 对齐：内置目标 UDP；端口为 0 或未启用时为 ``None``。"""
		return self._object_udp

	def configure_navigation_tcp(
		self,
		ip: str = DEFAULT_NAV_IP,
		port: int = DEFAULT_NAV_PORT,
		*,
		timeout_s: float = 5.0,
		msg_type: int = DEFAULT_NAV_MSG_TYPE,
	) -> "LinglongHSdkClass":
		"""配置导航TCP目标。

		msg_type 默认 3051，通常无需改动。
		"""
		self.nav_target = (str(ip), int(port))
		self.nav_timeout_s = max(0.05, float(timeout_s))
		self.nav_msg_type = int(msg_type) & 0xFFFF
		return self

	@staticmethod
	def _build_seer_header(req_id: int, msg_type: int, json_len: int, reserved6: bytes | None = None) -> bytes:
		"""构造对应协议数据。"""
		header = bytearray(16)
		header[0] = 0x5A
		header[1] = 0x01
		header[2:4] = struct.pack(">H", int(req_id) & 0xFFFF)
		header[4:8] = struct.pack(">I", int(json_len) & 0xFFFFFFFF)
		header[8:10] = struct.pack(">H", int(msg_type) & 0xFFFF)
		if reserved6 is not None:
			r = bytes(reserved6)[:6]
			header[10 : 10 + len(r)] = r
		return bytes(header)

	@staticmethod
	def _recv_exact(sock: socket.socket, n: int) -> bytes:
		"""内部辅助函数。"""
		buf = bytearray()
		while len(buf) < n:
			chunk = sock.recv(n - len(buf))
			if not chunk:
				raise OSError("TCP连接中断，接收不完整")
			buf.extend(chunk)
		return bytes(buf)

	def _build_navigation_request(self, req_id: int, json_body: str) -> bytes:
		"""构造对应协议数据。"""
		body = json_body.encode("utf-8")
		head = self._build_seer_header(req_id, self.nav_msg_type, len(body))
		return head + body

	def send_navigation_simple(
		self,
		target_id: str,
		*,
		source_id: str = "SELF_POSITION",
		task_id: str = "",
	) -> tuple[bool, str]:
		"""简单导航：source_id -> target_id，返回(ok, response_text)。

		ok 判定规则：响应 JSON 中 ret_code == 0。
		"""
		target_id = str(target_id).strip()
		if not target_id:
			raise ValueError("target_id 不能为空")
		req_id = self.nav_req_id
		if not task_id:
			task_id = f"nav_{target_id.lower()}_{req_id}"
		body = json.dumps(
			{
				"source_id": str(source_id),
				"id": target_id,
				"task_id": str(task_id),
			},
			separators=(",", ":"),
			ensure_ascii=False,
		)
		request = self._build_navigation_request(req_id, body)
		self.nav_req_id = (self.nav_req_id + 1) & 0xFFFF
		if self.nav_req_id == 0:
			self.nav_req_id = 1

		with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
			sock.settimeout(float(self.nav_timeout_s))
			sock.connect(self.nav_target)
			sent = sock.send(request)
			if sent != len(request):
				raise OSError("导航请求发送不完整")
			head = self._recv_exact(sock, 16)
			json_len = struct.unpack(">I", head[4:8])[0]
			response_text = "{}"
			if json_len > 0:
				response_text = self._recv_exact(sock, int(json_len)).decode("utf-8", errors="replace")

		try:
			fields = json.loads(response_text)
		except json.JSONDecodeError:
			fields = {}

		ret_code = fields.get("ret_code", -1)
		try:
			ret_code = int(ret_code)
		except (TypeError, ValueError):
			ret_code = -1
		return (ret_code == 0), response_text

	def _state_thread_loop(self):
		# 后台持续状态同步线程：内部复用 base 的 _exchange_state_once。
		"""后台状态线程主循环。"""
		while not self._state_stop.is_set():
			try:
				self._exchange_state_once(self._state_poll_timeout)
			except OSError:
				break
			except Exception:
				continue

	def _debug_thread_loop(self):
		# 10Hz 打印 ctrl/sens，便于联调观察当前目标与反馈。
		"""后台调试打印线程主循环。"""
		period = 0.1  # 10Hz
		while not self._debug_stop.is_set():
			print("===== LinglongHSdk Debug (10Hz) =====")
			self.ctrl.print()
			t = self._state_thread
			if t is not None and t.is_alive():
				self.sens.print()
			time.sleep(period)

	@staticmethod
	def _pose_to_rotation_matrix(euler_angles_xyz: np.ndarray) -> np.ndarray:
		"""Rz(yaw) * Ry(pitch) * Rx(roll)."""
		roll, pitch, yaw = euler_angles_xyz
		cr, sr = np.cos(roll), np.sin(roll)
		cp, sp = np.cos(pitch), np.sin(pitch)
		cy, sy = np.cos(yaw), np.sin(yaw)

		rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]])
		ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]])
		rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]])
		return rz @ ry @ rx

	@staticmethod
	def _rotation_to_pose_nei(rotation_matrix: np.ndarray) -> np.ndarray:
		"""内部辅助函数。"""
		sin_pitch = -rotation_matrix[2, 0]
		pitch = np.arcsin(np.clip(sin_pitch, -1.0, 1.0))
		if abs(rotation_matrix[2, 0]) < 0.999999:
			roll = np.arctan2(rotation_matrix[2, 1], rotation_matrix[2, 2])
			yaw = np.arctan2(rotation_matrix[1, 0], rotation_matrix[0, 0])
		else:
			roll = 0.0
			yaw = np.arctan2(-rotation_matrix[0, 1], rotation_matrix[1, 1])
		return np.array([roll, pitch, yaw], dtype=float)

	@staticmethod
	def _quaternion_from_matrix(m: np.ndarray) -> np.ndarray:
		"""内部辅助函数。"""
		trace = m[0, 0] + m[1, 1] + m[2, 2]
		if trace > 0.0:
			s = np.sqrt(trace + 1.0) * 2.0
			w = 0.25 * s
			x = (m[2, 1] - m[1, 2]) / s
			y = (m[0, 2] - m[2, 0]) / s
			z = (m[1, 0] - m[0, 1]) / s
		elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
			s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
			w = (m[2, 1] - m[1, 2]) / s
			x = 0.25 * s
			y = (m[0, 1] + m[1, 0]) / s
			z = (m[0, 2] + m[2, 0]) / s
		elif m[1, 1] > m[2, 2]:
			s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
			w = (m[0, 2] - m[2, 0]) / s
			x = (m[0, 1] + m[1, 0]) / s
			y = 0.25 * s
			z = (m[1, 2] + m[2, 1]) / s
		else:
			s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
			w = (m[1, 0] - m[0, 1]) / s
			x = (m[0, 2] + m[2, 0]) / s
			y = (m[1, 2] + m[2, 1]) / s
			z = 0.25 * s
		q = np.array([w, x, y, z], dtype=float)
		return q / np.linalg.norm(q)

	@staticmethod
	def _matrix_from_quaternion(q: np.ndarray) -> np.ndarray:
		"""内部辅助函数。"""
		w, x, y, z = q / np.linalg.norm(q)
		return np.array(
			[
				[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
				[2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
				[2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
			],
			dtype=float,
		)

	@staticmethod
	def _quaternion_slerp(q0: np.ndarray, q1: np.ndarray, t: float) -> np.ndarray:
		"""内部辅助函数。"""
		q0 = q0 / np.linalg.norm(q0)
		q1 = q1 / np.linalg.norm(q1)
		dot = float(np.dot(q0, q1))
		if dot < 0.0:
			q1 = -q1
			dot = -dot
		if dot > 0.9995:
			q = q0 + t * (q1 - q0)
			return q / np.linalg.norm(q)
		theta_0 = np.arccos(np.clip(dot, -1.0, 1.0))
		sin_theta_0 = np.sin(theta_0)
		theta = theta_0 * t
		sin_theta = np.sin(theta)
		s0 = np.sin(theta_0 - theta) / sin_theta_0
		s1 = sin_theta / sin_theta_0
		return s0 * q0 + s1 * q1

	@classmethod
	def _eef_interp_pose(cls, p0: np.ndarray, p1: np.ndarray, t: float) -> np.ndarray:
		"""eef[6]=[x,y,z,roll,pitch,yaw]，位置线性+姿态slerp。"""
		pose0, rot0 = p0[:3], p0[3:]
		pose1, rot1 = p1[:3], p1[3:]
		mat0 = cls._pose_to_rotation_matrix(rot0)
		mat1 = cls._pose_to_rotation_matrix(rot1)
		quat0 = cls._quaternion_from_matrix(mat0)
		quat1 = cls._quaternion_from_matrix(mat1)
		pos = (1.0 - t) * pose0 + t * pose1
		quat = cls._quaternion_slerp(quat0, quat1, t)
		rot = cls._matrix_from_quaternion(quat)
		rpy = cls._rotation_to_pose_nei(rot)
		return np.concatenate([pos, rpy])

	@classmethod
	def _interp_rpy_slerp(cls, rpy0: np.ndarray, rpy1: np.ndarray, t: float) -> np.ndarray:
		"""内部辅助函数。"""
		mat0 = cls._pose_to_rotation_matrix(rpy0)
		mat1 = cls._pose_to_rotation_matrix(rpy1)
		quat0 = cls._quaternion_from_matrix(mat0)
		quat1 = cls._quaternion_from_matrix(mat1)
		quat = cls._quaternion_slerp(quat0, quat1, t)
		rot = cls._matrix_from_quaternion(quat)
		return cls._rotation_to_pose_nei(rot)

	@staticmethod
	def _normalize_eef_keyframe(kf):
		"""兼容 (d,l,r) / (d,l,r,waist_pos,waist_att) / (d,l,r,waist_pos,waist_att,head_att,cap_l,cap_r)。"""
		if len(kf) == 3:
			duration, eef_l, eef_r = kf
			waist_pos = [0.0, 0.0, 0.0]
			waist_att = [0.0, 0.0, 0.0]
			head_att = [0.0, 0.1745, 0.0]
			cap_l = 0.0
			cap_r = 0.0
		elif len(kf) == 5:
			duration, eef_l, eef_r, waist_pos, waist_att = kf
			head_att = [0.0, 0.1745, 0.0]
			cap_l = 0.0
			cap_r = 0.0
		elif len(kf) == 8:
			duration, eef_l, eef_r, waist_pos, waist_att, head_att, cap_l, cap_r = kf
		else:
			raise ValueError("EEF关键帧格式错误")
		return (
			float(duration),
			np.array(eef_l, dtype=float),
			np.array(eef_r, dtype=float),
			np.array(waist_pos, dtype=float),
			np.array(waist_att, dtype=float),
			np.array(head_att, dtype=float),
			float(cap_l),
			float(cap_r),
		)

	def interpolate_eef_keyframes(self, keyframes_with_durations, target_time: float, loop: bool = False, total_time: float | None = None):
		"""末端插值：双臂eef用位置线性+姿态slerp，腰姿态也用slerp。"""
		kfs = [self._normalize_eef_keyframe(kf) for kf in keyframes_with_durations]
		if len(kfs) == 0:
			return None, None, None, None, None, None, None
		if len(kfs) == 1:
			k = kfs[0]
			return k[1].tolist(), k[2].tolist(), k[3].tolist(), k[4].tolist(), k[5].tolist(), float(k[6]), float(k[7])
		if total_time is None:
			total_time = sum(k[0] for k in (kfs if loop else kfs[:-1]))
		if loop and total_time and total_time > 0:
			target_time = target_time % total_time
		if target_time <= 0:
			k = kfs[0]
			return k[1].tolist(), k[2].tolist(), k[3].tolist(), k[4].tolist(), k[5].tolist(), float(k[6]), float(k[7])

		cum = [0.0]
		for i in range(len(kfs) - 1):
			cum.append(cum[-1] + kfs[i][0])
		seg_n = len(cum) - 1
		for i in range(seg_n):
			t0, t1 = cum[i], cum[i + 1]
			if t0 <= target_time <= t1:
				u = (target_time - t0) / (t1 - t0) if t1 > t0 else 0.0
				si = i
				ei = 0 if (i == seg_n - 1 and loop) else (i + 1)
				eef_l = self._eef_interp_pose(kfs[si][1], kfs[ei][1], u)
				eef_r = self._eef_interp_pose(kfs[si][2], kfs[ei][2], u)
				waist_pos = (1.0 - u) * kfs[si][3] + u * kfs[ei][3]
				waist_att = self._interp_rpy_slerp(kfs[si][4], kfs[ei][4], u)
				head_att = (1.0 - u) * kfs[si][5] + u * kfs[ei][5]
				cap_l = (1.0 - u) * kfs[si][6] + u * kfs[ei][6]
				cap_r = (1.0 - u) * kfs[si][7] + u * kfs[ei][7]
				return eef_l.tolist(), eef_r.tolist(), waist_pos.tolist(), waist_att.tolist(), head_att.tolist(), float(cap_l), float(cap_r)

		k = kfs[-1]
		return k[1].tolist(), k[2].tolist(), k[3].tolist(), k[4].tolist(), k[5].tolist(), float(k[6]), float(k[7])

	@staticmethod
	def _normalize_joint_keyframe(kf):
		"""兼容 (d,arm_l,arm_r) / (d,arm_l,arm_r,waist_q,head_q,cap_l,cap_r)。"""
		if len(kf) == 3:
			duration, arm_l, arm_r = kf
			waist = [0.0] * 4
			head = [0.0] * 2
			cap_l = 0.0
			cap_r = 0.0
		elif len(kf) == 7:
			duration, arm_l, arm_r, waist, head, cap_l, cap_r = kf
		else:
			raise ValueError("关节关键帧格式错误")
		return (
			float(duration),
			np.array(arm_l, dtype=float),
			np.array(arm_r, dtype=float),
			np.array(waist, dtype=float),
			np.array(head, dtype=float),
			float(cap_l),
			float(cap_r),
		)

	def interpolate_joint_keyframes(self, keyframes_with_durations, target_time: float, loop: bool = False, total_time: float | None = None):
		"""关节插值：分段线性插值 arm/waist/head/cap。"""
		kfs = [self._normalize_joint_keyframe(kf) for kf in keyframes_with_durations]
		if len(kfs) < 2:
			if len(kfs) == 1:
				k = kfs[0]
				return k[1].tolist(), k[2].tolist(), k[3].tolist(), k[4].tolist(), float(k[5]), float(k[6])
			return None
		if total_time is None:
			total_time = sum(k[0] for k in (kfs if loop else kfs[:-1]))
		if loop and total_time and total_time > 0:
			target_time = target_time % total_time

		acc = 0.0
		for i in range(len(kfs) - 1):
			d = kfs[i][0]
			if acc <= target_time <= acc + d:
				u = (target_time - acc) / d if d > 0 else 0.0
				a, b = kfs[i], kfs[i + 1]
				arm_l = (1.0 - u) * a[1] + u * b[1]
				arm_r = (1.0 - u) * a[2] + u * b[2]
				waist = (1.0 - u) * a[3] + u * b[3]
				head = (1.0 - u) * a[4] + u * b[4]
				cap_l = (1.0 - u) * a[5] + u * b[5]
				cap_r = (1.0 - u) * a[6] + u * b[6]
				return arm_l.tolist(), arm_r.tolist(), waist.tolist(), head.tolist(), float(cap_l), float(cap_r)
			acc += d

		k = kfs[-1]
		return k[1].tolist(), k[2].tolist(), k[3].tolist(), k[4].tolist(), float(k[5]), float(k[6])

	@staticmethod
	def _head_q_to_att(head_q: np.ndarray) -> np.ndarray:
		"""head_q(2) -> head_att(3)，与轨迹宽行 / sync一致: [0, q1, q0]。"""
		return np.array([0.0, float(head_q[1]), float(head_q[0])], dtype=float)

	def _current_joint_start(self):
		"""从当前 sens 读取关节空间起点。"""
		arm_l = np.array(self.sens.q[0], dtype=float)
		arm_r = np.array(self.sens.q[1], dtype=float)
		waist = np.array(self.sens.waist, dtype=float)
		head = np.array(self.sens.head, dtype=float)
		cap_l = float(self.sens.cap_rate[0])
		cap_r = float(self.sens.cap_rate[1])
		return arm_l, arm_r, waist, head, cap_l, cap_r

	def _current_eef_start(self):
		"""从当前 sens 读取末端空间起点。"""
		eef_l = np.array(self.sens.epos_h[0][:6], dtype=float)
		eef_r = np.array(self.sens.epos_h[1][:6], dtype=float)
		waist_pos = np.array(self.sens.epos_waist[:3], dtype=float)
		waist_att = np.array(self.sens.epos_waist[3:6], dtype=float)
		head_att = self._head_q_to_att(np.array(self.sens.head, dtype=float))
		cap_l = float(self.sens.cap_rate[0])
		cap_r = float(self.sens.cap_rate[1])
		return eef_l, eef_r, waist_pos, waist_att, head_att, cap_l, cap_r

	def _current_joint_start_from_ctrl(self):
		"""从当前 ctrl 读取关节插值起点（不依赖状态口）。"""
		arm_l = np.array(self.ctrl.arm_q_exp_l, dtype=float).reshape(-1)
		arm_r = np.array(self.ctrl.arm_q_exp_r, dtype=float).reshape(-1)
		waist = np.array(self.ctrl.waist_q_exp, dtype=float).reshape(-1)
		head = np.array(self.ctrl.head_q_exp, dtype=float).reshape(-1)
		cap_l = float(self.ctrl.cap_l)
		cap_r = float(self.ctrl.cap_r)
		return arm_l, arm_r, waist, head, cap_l, cap_r

	def _current_eef_start_from_ctrl(self):
		"""从当前 ctrl 读取末端插值起点（不依赖状态口）。"""
		eef_l = np.concatenate(
			[np.asarray(self.ctrl.arm_pos_exp_l, dtype=float).reshape(-1)[:3],
			 np.asarray(self.ctrl.arm_att_exp_l, dtype=float).reshape(-1)[:3]]
		)
		eef_r = np.concatenate(
			[np.asarray(self.ctrl.arm_pos_exp_r, dtype=float).reshape(-1)[:3],
			 np.asarray(self.ctrl.arm_att_exp_r, dtype=float).reshape(-1)[:3]]
		)
		waist_pos = np.array(self.ctrl.waist_pos_exp, dtype=float).reshape(-1)
		waist_att = np.array(self.ctrl.waist_att_exp, dtype=float).reshape(-1)
		head_att = np.array(self.ctrl.head_att_exp, dtype=float).reshape(-1)
		cap_l = float(self.ctrl.cap_l)
		cap_r = float(self.ctrl.cap_r)
		return eef_l, eef_r, waist_pos, waist_att, head_att, cap_l, cap_r

	def _ensure_send_sequence_start(self) -> int:
		"""发送类函数入口检查：暂停时禁止启动。"""
		if self.is_mani_send_paused():
			raise RuntimeError("当前处于 pause 状态，禁止启动发送类函数")
		return self.get_send_interrupt_seq()

	def _should_abort_send_sequence(self, start_seq: int) -> bool:
		"""运行中检查：pause 或模式切换触发中断时立即退出。"""
		if self.is_mani_send_paused():
			return True
		return self.is_send_interrupted_since(start_seq)

	def _fatal_reset_state_timeout_shutdown(self, message: str) -> None:
		"""``reset_to_init`` 在 ``kStatus`` 下等不到状态：摇操 -> 下使能 -> 关闭 SDK，再抛错。"""
		mgr = RobotModeManager(self.rbtIpPort[0], DEFAULT_MODE_PORT)
		try:
			mgr.robot_operation_mode()
			time.sleep(1.0)
			mgr.robot_enable_down()
			time.sleep(5.0)
		finally:
			mgr.close()
		self.close()
		raise RuntimeError(message)

	def send_joint_interpolation(
		self,
		target_arm_q_l,
		target_arm_q_r,
		send_time: float,
		target_waist_q=None,
		target_head_q=None,
		target_cap_l: float | None = None,
		target_cap_r: float | None = None,
		interp_start: ManiInterpStartSource | str | int = ManiInterpStartSource.kStatus,
	):
		"""固定50Hz关节插值发送。interp_start 默认 ``kStatus``（从 sens）；``kCtrl`` 从当前 ctrl。"""
		start_seq = self._ensure_send_sequence_start()
		send_time = max(0.0, float(send_time))
		if _interp_start_mode(interp_start) == "ctrl":
			s_arm_l, s_arm_r, s_waist, s_head, s_cap_l, s_cap_r = self._current_joint_start_from_ctrl()
		else:
			self.fetch_robot_state(timeout=max(0.05, self._state_poll_timeout * 2.0))
			s_arm_l, s_arm_r, s_waist, s_head, s_cap_l, s_cap_r = self._current_joint_start()

		t_arm_l = np.asarray(target_arm_q_l, dtype=float).reshape(-1)
		t_arm_r = np.asarray(target_arm_q_r, dtype=float).reshape(-1)
		if t_arm_l.size != 7 or t_arm_r.size != 7:
			raise ValueError("target_arm_q_l / target_arm_q_r 必须为长度7")
		t_waist = s_waist if target_waist_q is None else np.asarray(target_waist_q, dtype=float).reshape(-1)
		t_head = s_head if target_head_q is None else np.asarray(target_head_q, dtype=float).reshape(-1)
		if t_waist.size != 4:
			raise ValueError("target_waist_q 必须为长度4")
		if t_head.size != 2:
			raise ValueError("target_head_q 必须为长度2")
		t_cap_l = s_cap_l if target_cap_l is None else float(target_cap_l)
		t_cap_r = s_cap_r if target_cap_r is None else float(target_cap_r)

		keyframes = [
			(send_time, s_arm_l, s_arm_r, s_waist, s_head, s_cap_l, s_cap_r),
			(0.0, t_arm_l, t_arm_r, t_waist, t_head, t_cap_l, t_cap_r),
		]

		period = 1.0 / 50.0
		t = 0.0
		while t <= send_time:
			if self._should_abort_send_sequence(start_seq):
				return False
			frame_start = time.time()
			arm_l, arm_r, waist, head, cap_l, cap_r = self.interpolate_joint_keyframes(keyframes, t, loop=False, total_time=send_time)
			self.ctrl.arm_q_exp_l[:] = np.asarray(arm_l, dtype=np.float32)
			self.ctrl.arm_q_exp_r[:] = np.asarray(arm_r, dtype=np.float32)
			self.ctrl.waist_q_exp[:] = np.asarray(waist, dtype=np.float32)
			self.ctrl.head_q_exp[:] = np.asarray(head, dtype=np.float32)
			self.sync_head_att_from_joint()
			self.ctrl.cap_l = float(cap_l)
			self.ctrl.cap_r = float(cap_r)
			if self._should_abort_send_sequence(start_seq):
				return False
			self.send()
			elapsed = time.time() - frame_start
			sleep_t = period - elapsed
			if sleep_t > 0:
				time.sleep(sleep_t)
			t += period

		if self._should_abort_send_sequence(start_seq):
			return False
		self.ctrl.arm_q_exp_l[:] = np.asarray(t_arm_l, dtype=np.float32)
		self.ctrl.arm_q_exp_r[:] = np.asarray(t_arm_r, dtype=np.float32)
		self.ctrl.waist_q_exp[:] = np.asarray(t_waist, dtype=np.float32)
		self.ctrl.head_q_exp[:] = np.asarray(t_head, dtype=np.float32)
		self.sync_head_att_from_joint()
		self.ctrl.cap_l = float(t_cap_l)
		self.ctrl.cap_r = float(t_cap_r)
		self.send()
		return True

	def send_eef_interpolation(
		self,
		target_eef_l,
		target_eef_r,
		send_time: float,
		target_waist_pos=None,
		target_waist_att=None,
		target_head_att=None,
		target_cap_l: float | None = None,
		target_cap_r: float | None = None,
		interp_start: ManiInterpStartSource | str | int = ManiInterpStartSource.kStatus,
	):
		"""固定50Hz末端插值发送。interp_start 默认 ``kStatus``（从 sens）；``kCtrl`` 从当前 ctrl。"""
		start_seq = self._ensure_send_sequence_start()
		send_time = max(0.0, float(send_time))
		if _interp_start_mode(interp_start) == "ctrl":
			s_l, s_r, s_w_pos, s_w_att, s_h_att, s_cap_l, s_cap_r = self._current_eef_start_from_ctrl()
		else:
			self.fetch_robot_state(timeout=max(0.05, self._state_poll_timeout * 2.0))
			s_l, s_r, s_w_pos, s_w_att, s_h_att, s_cap_l, s_cap_r = self._current_eef_start()

		t_l = np.asarray(target_eef_l, dtype=float).reshape(-1)
		t_r = np.asarray(target_eef_r, dtype=float).reshape(-1)
		if t_l.size != 6 or t_r.size != 6:
			raise ValueError("target_eef_l / target_eef_r 必须为长度6 [x,y,z,r,p,y]")
		t_w_pos = s_w_pos if target_waist_pos is None else np.asarray(target_waist_pos, dtype=float).reshape(-1)
		t_w_att = s_w_att if target_waist_att is None else np.asarray(target_waist_att, dtype=float).reshape(-1)
		t_h_att = s_h_att if target_head_att is None else np.asarray(target_head_att, dtype=float).reshape(-1)
		if t_w_pos.size != 3:
			raise ValueError("target_waist_pos 必须为长度3")
		if t_w_att.size != 3:
			raise ValueError("target_waist_att 必须为长度3")
		if t_h_att.size != 3:
			raise ValueError("target_head_att 必须为长度3")
		t_cap_l = s_cap_l if target_cap_l is None else float(target_cap_l)
		t_cap_r = s_cap_r if target_cap_r is None else float(target_cap_r)

		keyframes = [
			(send_time, s_l, s_r, s_w_pos, s_w_att, s_h_att, s_cap_l, s_cap_r),
			(0.0, t_l, t_r, t_w_pos, t_w_att, t_h_att, t_cap_l, t_cap_r),
		]

		period = 1.0 / 50.0
		t = 0.0
		while t <= send_time:
			if self._should_abort_send_sequence(start_seq):
				return False
			frame_start = time.time()
			eef_l, eef_r, waist_pos, waist_att, head_att, cap_l, cap_r = self.interpolate_eef_keyframes(keyframes, t, loop=False, total_time=send_time)
			self.ctrl.arm_pos_exp_l[:] = np.asarray(eef_l[:3], dtype=np.float32)
			self.ctrl.arm_att_exp_l[:] = np.asarray(eef_l[3:], dtype=np.float32)
			self.ctrl.arm_pos_exp_r[:] = np.asarray(eef_r[:3], dtype=np.float32)
			self.ctrl.arm_att_exp_r[:] = np.asarray(eef_r[3:], dtype=np.float32)
			self.ctrl.waist_pos_exp[:] = np.asarray(waist_pos, dtype=np.float32)
			self.ctrl.waist_att_exp[:] = np.asarray(waist_att, dtype=np.float32)
			self.ctrl.head_att_exp[:] = np.asarray(head_att, dtype=np.float32)
			self.ctrl.cap_l = float(cap_l)
			self.ctrl.cap_r = float(cap_r)
			if self._should_abort_send_sequence(start_seq):
				return False
			self.send()
			elapsed = time.time() - frame_start
			sleep_t = period - elapsed
			if sleep_t > 0:
				time.sleep(sleep_t)
			t += period

		if self._should_abort_send_sequence(start_seq):
			return False
		self.ctrl.arm_pos_exp_l[:] = np.asarray(t_l[:3], dtype=np.float32)
		self.ctrl.arm_att_exp_l[:] = np.asarray(t_l[3:], dtype=np.float32)
		self.ctrl.arm_pos_exp_r[:] = np.asarray(t_r[:3], dtype=np.float32)
		self.ctrl.arm_att_exp_r[:] = np.asarray(t_r[3:], dtype=np.float32)
		self.ctrl.waist_pos_exp[:] = np.asarray(t_w_pos, dtype=np.float32)
		self.ctrl.waist_att_exp[:] = np.asarray(t_w_att, dtype=np.float32)
		self.ctrl.head_att_exp[:] = np.asarray(t_h_att, dtype=np.float32)
		self.ctrl.cap_l = float(t_cap_l)
		self.ctrl.cap_r = float(t_cap_r)
		self.send()
		return True

	def send_interpolation(self, mode: str, send_time: float, **kwargs):
		"""统一入口：mode='joint' 或 'eef'，固定50Hz。"""
		m = str(mode).lower().strip()
		if m == "joint":
			return self.send_joint_interpolation(send_time=send_time, **kwargs)
		if m == "eef":
			return self.send_eef_interpolation(send_time=send_time, **kwargs)
		raise ValueError("mode 仅支持 'joint' 或 'eef'")

	def reset_to_init(
		self,
		send_time: float = 2.0,
		mode: str = "eef",
		interp_start: ManiInterpStartSource | str | int = ManiInterpStartSource.kStatus,
		state_wait_timeout_s: float = 2.0,
		state_wait_poll_s: float = 0.05,
	) -> bool:
		"""从当前状态插值复位到固定 init 位姿。

		- mode='eef': 复位到默认末端/腰/头姿态（推荐）
		- mode='joint': 复位到默认关节目标
		- interp_start: ``ManiInterpStartSource.kStatus``（默认）或 ``kCtrl``，与 send_*_interpolation 一致
		- ``kStatus`` 时先 ``wait_for_first_state_udp``；超时则 **摇操 -> 下使能 -> 关闭 SDK** 并抛出 ``RuntimeError``
		"""
		if _interp_start_mode(interp_start) == "status":
			if not self.wait_for_first_state_udp(state_wait_timeout_s, state_wait_poll_s):
				self._fatal_reset_state_timeout_shutdown(
					"reset_to_init: kStatus 下超时未收到合法状态 UDP（请检查 IP、状态口与网络）"
				)
		start_seq = self._ensure_send_sequence_start()
		m = str(mode).lower().strip()
		ok = True
		if m == "eef":
			self.set_mode(1)
			ok = self.send_eef_interpolation(
				target_eef_l=[0.3, 0.25, 0.65, 0.0, 0.0, 0.0],
				target_eef_r=[0.3, -0.25, 0.65, 0.0, 0.0, 0.0],
				target_waist_pos=[0.0, 0.0, 0.8],
				target_waist_att=[0.0, 0.0, 0.0],
				target_head_att=[0.0, 0.1745, 0.0],
				target_cap_l=0.0,
				target_cap_r=0.0,
				send_time=send_time,
				interp_start=interp_start,
			)
		elif m == "joint":
			self.set_mode(0)
			ok = self.send_joint_interpolation(
				target_arm_q_l=[0.0, 0.35, 0.0, 0.0, 0.0, 0.0, 0.0],
				target_arm_q_r=[0.0, -0.35, 0.0, 0.0, 0.0, 0.0, 0.0],
				target_waist_q=[-0.2, 0.4, -0.2, 0.0],
				target_head_q=[0.0, 0.1745],
				target_cap_l=0.0,
				target_cap_r=0.0,
				send_time=send_time,
				interp_start=interp_start,
			)
		else:
			raise ValueError("mode 仅支持 'eef' 或 'joint'")
		if not ok or self._should_abort_send_sequence(start_seq):
			return False

		# 复位后把底盘目标速度归零并发送一帧
		self.set_base_vel(0.0, 0.0)
		self.send()
		return True

	@classmethod
	def _csv_make_zero_frame(cls):
		"""处理CSV回放相关数据。"""
		d = {name: [0.0] * length for name, length in cls.CSV_SIGNAL_LENGTHS.items()}
		d["head_q"] = [0.0, 0.1745]
		return d

	@classmethod
	def _csv_make_partial_frame(cls):
		"""处理CSV回放相关数据。"""
		return {name: [None] * length for name, length in cls.CSV_SIGNAL_LENGTHS.items()}

	@classmethod
	def _csv_load_raw_frames(cls, csv_path: str, value_field: str):
		"""处理CSV回放相关数据。"""
		partial_frames = defaultdict(cls._csv_make_partial_frame)
		with open(csv_path, newline="") as f:
			reader = csv.DictReader(f)
			for row in reader:
				ts = int(row["timestamp"])
				name = row["name"]
				idx = int(row["index"])
				value = float(row[value_field])
				if name not in cls.CSV_SIGNAL_LENGTHS:
					continue
				if not (0 <= idx < cls.CSV_SIGNAL_LENGTHS[name]):
					continue
				partial_frames[ts][name][idx] = value
		return sorted(partial_frames.items())

	@classmethod
	def _csv_merge_partial_frames(cls, raw_frames):
		"""处理CSV回放相关数据。"""
		frames = []
		current = cls._csv_make_zero_frame()
		for ts, partial in raw_frames:
			for name, values in partial.items():
				for idx, value in enumerate(values):
					if value is not None:
						current[name][idx] = value
			frames.append((ts, {name: values[:] for name, values in current.items()}))
		return frames

	def _csv_frame_to_ctrl(self, frame):
		# CSV 回放帧只写“期望值(exp)”；底盘实际速度由状态链路回填到 status。
		"""处理CSV回放相关数据。"""
		self.ctrl.arm_pos_exp_l[:] = np.asarray(frame["ee_pose_l"][:3], dtype=np.float32)
		self.ctrl.arm_att_exp_l[:] = np.asarray(frame["ee_pose_l"][3:6], dtype=np.float32)
		self.ctrl.arm_q_exp_l[:] = np.asarray(frame["arm_q_l"][:], dtype=np.float32)
		self.ctrl.cap_l = float(frame["cap"][0])

		self.ctrl.arm_pos_exp_r[:] = np.asarray(frame["ee_pose_r"][:3], dtype=np.float32)
		self.ctrl.arm_att_exp_r[:] = np.asarray(frame["ee_pose_r"][3:6], dtype=np.float32)
		self.ctrl.arm_q_exp_r[:] = np.asarray(frame["arm_q_r"][:], dtype=np.float32)
		self.ctrl.cap_r = float(frame["cap"][1])

		self.ctrl.waist_pos_exp[:] = np.asarray(frame["ee_pose_waist"][:3], dtype=np.float32)
		self.ctrl.waist_att_exp[:] = np.asarray(frame["ee_pose_waist"][3:6], dtype=np.float32)
		self.ctrl.waist_q_exp[:] = np.asarray(frame["waist_q"][:], dtype=np.float32)

		self.ctrl.head_q_exp[:] = np.asarray(frame["head_q"][:], dtype=np.float32)
		self.ctrl.head_att_exp[:] = np.asarray(
			[0.0, frame["head_q"][1], frame["head_q"][0]],
			dtype=np.float32,
		)

		self.ctrl.car_translation_exp = float(frame["base_vel"][0])
		self.ctrl.car_rotation_exp = float(frame["base_vel"][1])

	def playback_csv(
		self,
		csv_file: str,
		*,
		mode: int = 0,
		value_field: str = "status",
		loop: bool = False,
		speed: float = 1.0,
		print_every: int = 50,
		send_chassis_tcp: bool = False,
	):
		"""按 CSV 时间戳回放整帧控制数据到 SDK。

		- mode=0: 角度模式（joint）
		- mode=1: 末端模式（eef）
		- send_chassis_tcp=True: 在当前 send() 之外额外发送一次底盘TCP（通常不需要）
		CSV 列格式: timestamp,name,index,status,exp
		"""
		if mode not in (0, 1):
			raise ValueError("mode 仅支持 0(角度) 或 1(末端)")
		if value_field not in ("status", "exp"):
			raise ValueError("value_field 仅支持 'status' 或 'exp'")
		if speed <= 0:
			raise ValueError("speed 必须 > 0")
		start_seq = self._ensure_send_sequence_start()

		raw_frames = self._csv_load_raw_frames(csv_file, value_field)
		if not raw_frames:
			raise ValueError("CSV 中没有可回放数据")
		frames = self._csv_merge_partial_frames(raw_frames)

		self.set_mode(mode)
		while True:
			for i, (ts, frame) in enumerate(frames):
				if self._should_abort_send_sequence(start_seq):
					return False
				self._csv_frame_to_ctrl(frame)
				if self._should_abort_send_sequence(start_seq):
					return False
				self.send()
				tcp_sent = 0
				# 仅当关闭了 send() 的底盘联动时，才允许在这里手动补发一次底盘TCP。
				manual_tcp = bool(send_chassis_tcp) and (not self.chassis_tcp_on_send)
				if manual_tcp:
					tcp_sent = self.send_chassis_command(
						self.ctrl.car_translation_exp,
						self.ctrl.car_rotation_exp,
					)

				if print_every > 0 and i % print_every == 0:
					msg = (
						f"[{i}] ts={ts} "
						f"L0={self.ctrl.arm_q_exp_l[0]:.3f} "
						f"R0={self.ctrl.arm_q_exp_r[0]:.3f} "
						f"eef_l_x={self.ctrl.arm_pos_exp_l[0]:.3f} "
						f"vx={self.ctrl.car_translation_exp:.4f} "
						f"w={self.ctrl.car_rotation_exp:.4f}"
					)
					if manual_tcp:
						msg += f" tcp_bytes={tcp_sent}"
					print(msg)

				if i < len(frames) - 1:
					next_ts = frames[i + 1][0]
					dt = max(0.0, (next_ts - ts) * 1e-9 / speed)
					time.sleep(dt)

			if not loop:
				break
		return True

	def wait_for_first_state_udp(
		self,
		total_timeout_s: float = 2.0,
		poll_timeout_s: float = 0.05,
	) -> bool:
		"""阻塞直到后台线程（或本类回退路径）经状态 UDP 成功收编至少一帧合法 ``sens``，或超时。

		有后台状态线程时**不**与线程争用 ``_exchange_state_once``，仅在条件变量上等待
		``_state_udp_received_ok`` 被置位（由 ``maniSdkClass._exchange_state_once`` 在解包成功后设置）。
		"""
		t = self._state_thread
		if t is not None and t.is_alive():
			deadline = time.monotonic() + max(0.0, float(total_timeout_s))
			poll = max(1e-3, float(poll_timeout_s))
			while time.monotonic() < deadline:
				if self._state_udp_received_ok:
					return True
				remain = deadline - time.monotonic()
				if remain <= 0:
					break
				with self._state_cv:
					self._state_cv.wait(timeout=min(poll, remain))
			return bool(self._state_udp_received_ok)
		return super().wait_for_first_state_udp(total_timeout_s, poll_timeout_s)

	def fetch_robot_state(self, timeout: float = 0.05) -> maniSdkSensDataClass:
		"""线程模式下优先等待后台更新；无线程时回退到 base 的主动拉取。"""
		t = self._state_thread
		if t is not None and t.is_alive():
			with self._state_cv:
				self._state_cv.wait(timeout=float(timeout))
			return self.sens
		return super().fetch_robot_state(timeout)

	def recv(self) -> maniSdkSensDataClass:
		"""线程模式下直接返回最近状态；无线程时回退到 base 的 UDP recv。"""
		t = self._state_thread
		if t is not None and t.is_alive():
			return self.sens
		return super().recv()

	def close(self):
		"""停止扩展线程并关闭底层资源。"""
		if self._object_udp is not None:
			self._object_udp.stop()
			self._object_udp = None
		self._debug_stop.set()
		td = self._debug_thread
		if td is not None and td.is_alive():
			td.join(timeout=1.0)
		self._debug_thread = None
		self._state_stop.set()
		with self._state_cv:
			self._state_cv.notify_all()
		t = self._state_thread
		if t is not None and t.is_alive():
			t.join(timeout=max(2.0, self._state_poll_timeout * 5))
		self._state_thread = None
		super().close()
