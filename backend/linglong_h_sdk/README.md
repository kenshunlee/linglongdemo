# linglong_h_sdk

灵龙-H Python SDK（UDP 控制 + UDP 状态 + 底盘 TCP + 导航 TCP）。

**客户集成指南（Python/C++，联调必读）**：[`docs/SDK_USAGE_CN.md`](../docs/SDK_USAGE_CN.md)。

本 README 按“接口说明书”风格组织，重点解释：

- 每个类负责什么
- 关键成员变量存什么
- 主要方法参数/返回值是什么
- 数据和状态在系统里怎么流转
- 代码里的中文注释该怎么看

---

## 1. 模块结构与职责边界

### `sdk_base.py`（基础层）

负责“可靠通信和基础控制”，不做高层轨迹策略：

- UDP 控制发送（3336）
- UDP 状态交换（3333）
- 底盘 TCP 速度发送（19205）
- 底盘 TCP 状态查询（速度 1005 + 电量 1007，19204）
- 机器人模式切换 UDP 管理（4141，独立类 `RobotModeManager`）
- 暂停/恢复发送和全局中断标记

### `sdk_extend.py`（扩展层）

在 `maniSdkClass` 基础上增加“高级行为”：

- 后台状态线程
- 调试线程（10Hz 打印）
- 末端/关节插值
- CSV 轨迹回放
- 导航 TCP（19206）
- 目标 UDP：`LinglongHSdkClass` 默认在 **5005** 启动物体表接收（`object_udp_receiver()`；**选用哪条名字**见 `traj_replan`）

### `sdk_trajectory.py`（轨迹任务与回放，`traj_replan` 子模块）

提供任务目录扫描、YAML/CSV 加载、双臂末端重规划与回放编排：

- 任务目录：`config_root/<任务名>/yaml/config.yaml` + `action/<name>.csv`
- 加载：`load_trajectory_task_from_config_directory`、`discover_trajectory_task_names`
- 长表 bag CSV → 宽行、`eef_interp` 段表 CSV
- **`eef_interp` 段表**：列数可为 **19/20、21/22、24/25**（末尾可选 **`post_segment_delay_s`**）；每段 `send_eef_interpolation` **成功**且仍存在下一 CSV 行时，先 **`sleep(post_segment_delay_s / max(speed, 1e-3))`**，与 C++ 轨迹回放一致；省略最后一列等价于 **0**。
- 双臂末端 sandwich 重规划（`older` YAML、`compute_sandwiched_replan_for_arm` 等）
- `resolve_live627_for_dual_arm_eef_replan` + `ObjectUdpReceiver`（`com_left/right`）
- 回放：`trajectory_playback_action_eef_interp`、`trajectory_playback_action_bag_wide`、`trajectory_playback_task`
- 启动横幅：`TrajectoryPlaybackRunBanner`（`interp_start_reset` / `interp_start_playback` 分别对应 `reset_to_init` 与轨迹段 `send_eef_interpolation` 的插值起点）、`print_trajectory_playback_run_banner`

依赖：**NumPy**（已有）、**PyYAML**（`pip install pyyaml`）。包内以子模块形式导出：`from linglong_h_sdk import traj_replan`。

**任务配置根目录**：仓库根目录下的 `config/<任务名>/`（本仓库示例与 `sdk_test` 默认使用该路径）。

---

## 2. 默认网络参数

### 机器人本体

- `DEFAULT_ROBOT_IP` = `192.168.1.28`
- `DEFAULT_MODE_PORT` = `4141`（模式切换）
- `DEFAULT_CMD_PORT` = `3336`（控制命令）
- `DEFAULT_STATE_PORT` = `3333`（状态交换）

### 底盘

- `DEFAULT_CHASSIS_IP` = `192.168.1.204`
- `DEFAULT_CHASSIS_CMD_PORT` = `19205`（速度命令）
- `DEFAULT_CHASSIS_STATE_PORT` = `19204`（状态查询）
- `DEFAULT_CHASSIS_CMD_TYPE` = `2010`（速度命令消息类型）
- `DEFAULT_CHASSIS_SPEED_QUERY_TYPE` = `1005`
- `DEFAULT_CHASSIS_BATTERY_QUERY_TYPE` = `1007`

### 导航（扩展层）

- `DEFAULT_NAV_IP` = `192.168.1.204`
- `DEFAULT_NAV_PORT` = `19206`
- `DEFAULT_NAV_MSG_TYPE` = `3051`

---

## 2.1 注释状态与阅读建议

当前版本已完成以下注释补充（对应你提出的“函数和成员都写中文说明”）：

- `sdk_base.py`
  - 各核心类 `__init__` 的成员变量已补中文注释（字段用途、单位、来源）
  - 所有函数均有中文 docstring（含内部辅助函数）
- `sdk_extend.py`
  - 各核心类 `__init__` 的成员变量已补中文注释（线程、导航、缓存等）
  - 所有函数均有中文 docstring（含插值、CSV、导航、接收线程）

建议阅读顺序：

1. 先看类的 `__init__`（快速建立成员变量语义）
2. 再看对外 API（`set_* / send / fetch_* / playback_* / navigation_*`）
3. 最后看内部 `_` 开头函数（协议细节和数学细节）

---

## 3. 数据类说明（字段语义）

## `maniSdkCtrlDataClass`（控制目标帧）

作用：对应下发控制包 `remote_msg_end` 的 payload（含 CRC 前主体）。

关键字段：

- `mode`：控制模式（`0` 关节 / `1` 末端）
- `arm_pos_exp_l/r`：左右臂末端位置期望 `[x,y,z]`
- `arm_att_exp_l/r`：左右臂末端姿态期望 `[roll,pitch,yaw]`
- `arm_q_exp_l/r`：左右臂 7 关节期望
- `waist_pos_exp` / `waist_att_exp` / `waist_q_exp`：腰部位置/姿态/4关节期望
- `head_att_exp` / `head_q_exp`：头部姿态/2关节期望
- `cap_l` / `cap_r`：夹爪开合
- `car_translation_exp` / `car_rotation_exp`：底盘期望线速度/角速度
- `car_translation_status` / `car_rotation_status`：底盘实际线速度/角速度（由状态链路回填）

说明：现在 `set_base_vel()` 只写 `*_exp`，`*_status` 由底盘查询更新。

## `maniSdkSensDataClass`（状态帧）

作用：对应 UDP 状态包 `116f + 1i` 的解析容器。

关键字段（按语义分组）：

- 关节：`q_exp` / `q` / `dq` / `tau`
- 末端：`epos_h` / `epos_exp` / `epos_waist` / `epos_exp_waist`
- 夹爪：`cap_rate_exp` / `cap_rate`
- 腰部：`waist` / `waist_exp`
- 头部：`head` / `head_exp`
- 底盘：`base_vel`（当前由底盘状态查询覆盖）
- 其他：`save_data`
- 扩展字段：`battery_level`（本地附加，不在 116f+i UDP 打包里）

## `RobotModeMessage`

作用：机器人模式切换 UDP 报文结构（`<4i`）。

字段：

- `enable`
- `disable`
- `retract_mode`
- `inference_teleop_mode`

---

## 4. 核心类详细说明

## `RobotModeManager`（只管模式切换）

职责：

- 独立于动作控制，持续发送模式切换包
- 切换模式时触发全局中断，使插值/回放主动结束

构造参数：

- `ip: str = DEFAULT_ROBOT_IP`
- `port: int = DEFAULT_MODE_PORT`
- `debug: bool = False`（10Hz 打印最近模式包）

主要成员变量：

- `rbtIpPort`：模式包 UDP 目的地址
- `sk`：UDP socket
- `_debug_last_msg`：用于 debug 线程打印的最近一帧

主要方法：

- `send_robot_mode(msg)`：发一帧模式包
- `robot_enable_up()`：上使能（按协议时序发送）
- `robot_enable_down()`：下使能
- `robot_operation_mode()`：摇操模式
- `robot_autonomous_mode()`：自主模式
- `robot_normal_mode()`：普通模式
- `robot_retract_mode()`：收拢模式
- `close()`：释放资源

## `maniSdkClass`（基础通信与控制）

职责：

- 管理 `ctrl`（命令目标）与 `sens`（状态）
- 提供所有基础 setter + `send()`
- 管理底盘命令 TCP 和底盘状态 TCP
- 提供暂停/恢复与中断语义

构造参数：

- `ip: str = DEFAULT_ROBOT_IP`
- `port: int = DEFAULT_CMD_PORT`
- `state_port: int = DEFAULT_STATE_PORT`
- `chassis_tcp_on_send: bool = True`

常用可读成员（对使用者最有价值）：

- `ctrl`：当前控制目标（你设置的期望值都在这里）
- `sens`：最近状态反馈（机器人/底盘反馈）
- `battery_level`：最近一次查询到的电量（0~1，未知为 `NaN`）

### `maniSdkClass` 主要 API（参数/返回）

#### 连接与配置

- `configure_chassis_tcp(ip, port, timeout_s=0.2, command_type=2010, request_builder=None) -> maniSdkClass`
  - `ip` / `port`：底盘速度命令 TCP 地址（默认 `192.168.1.204:19205`）
  - `timeout_s`：连接与发送超时（秒）
  - `command_type`：Seer 协议消息类型（默认 `2010`）
  - `request_builder`：自定义打包函数，签名 `fn(req_id, vx, w) -> bytes`
- `set_chassis_tcp_on_send(enabled: bool) -> maniSdkClass`
  - `enabled=True`：每次 `send()` 后自动发底盘 TCP 速度命令
- `configure_chassis_state_tcp(ip, port, timeout_s=0.05, query_type=1005, battery_query_type=1007) -> maniSdkClass`
  - `ip` / `port`：底盘状态查询 TCP 地址（默认 `192.168.1.204:19204`）
  - `timeout_s`：状态查询超时（秒）
  - `query_type`：速度查询消息类型（默认 `1005`）
  - `battery_query_type`：电量查询消息类型（默认 `1007`）

#### 控制 setter（均返回 `self`，参数含义）

- `set_mode(mode)`
  - `mode`：`0`=关节模式，`1`=末端模式
- `set_left_end(pos_xyz, rpy_xyz, relative=False)` / `set_right_end(...)` / `set_waist_end(...)`
  - `pos_xyz`：位置 `[x,y,z]`
  - `rpy_xyz`：姿态 `[roll,pitch,yaw]`
  - `relative=True`：相对当前目标叠加；`False`：绝对覆盖
- `set_left_arm_joint(q7, relative=False)` / `set_right_arm_joint(...)`
  - `q7`：7 关节角数组
- `set_waist_joint(q4, relative=False)`
  - `q4`：腰部 4 关节角数组
- `set_head_joint(q2, sync_att=False, relative=False)`
  - `q2`：头部 2 关节角
  - `sync_att=True`：自动同步 `head_att_exp`
- `set_head_att_exp(rpy_xyz, relative=False)`
  - `rpy_xyz`：头部姿态 `[roll,pitch,yaw]`
- `set_cap(cap_l, cap_r, relative=False)`
  - `cap_l` / `cap_r`：左右夹爪目标
- `set_base_vel(translation, rotation, relative=False)`
  - `translation`：底盘期望线速度 `vx`
  - `rotation`：底盘期望角速度 `w`
  - 注意：这里只写 `exp`，实际 `status` 由状态查询回填

#### 发送/状态

- `send(ctrl=None, force=False) -> None`
  - `ctrl=None`：发送当前 `self.ctrl`；否则发送传入控制对象
  - `force=True`：即使 pause 也允许发送（内部恢复流程使用）
  - 发 UDP 控制包（含 CRC）
  - 若 `chassis_tcp_on_send=True`，同步发底盘速度命令 TCP
  - 若已 pause 且 `force=False`，抛 `RuntimeError`
- `fetch_robot_state(timeout=0.05) -> maniSdkSensDataClass`
  - `timeout`：单轮状态交换超时（秒）
  - 单轮状态交换并更新内部状态
- `is_state_udp_received_ok() -> bool`
  - 是否已通过状态口成功解包至少一帧合法 `sens`
- `wait_for_first_state_udp(total_timeout_s=2.0, poll_timeout_s=0.05) -> bool`
  - 阻塞直到上述条件满足或超时；无后台状态线程时在本线程循环 `_exchange_state_once`
- `recv() -> maniSdkSensDataClass`
  - 从 UDP 尝试非阻塞接收一包状态
- `get_battery_level() -> float`
  - 返回最近电量（0~1，未知为 `NaN`）

#### 底盘状态查询

- `query_chassis_speed_state() -> tuple[float, float]`
  - 返回 `(vx, w)`
- `query_chassis_battery_level(simple=True) -> float`
  - `simple=True`：只请求简要电量字段
  - 返回 `battery_level`

#### 暂停/恢复

- `pause_mani_send(fetch_state=True, timeout=0.05)`
  - `fetch_state=True`：暂停前主动拉一次状态再做快照
  - `timeout`：拉状态超时（秒）
  - 进入暂停，后续 `send()` 被禁止
  - 记录当前实际姿态快照用于恢复回放
- `resume_mani_send(replay_s=1.0, hz=50.0)`
  - `replay_s`：恢复前快照回放时长（秒）
  - `hz`：回放发送频率
  - 先回放快照，再退出暂停

#### 中断状态（供插值/回放判断）

- `get_send_interrupt_seq() -> int`
- `is_send_interrupted_since(seq: int) -> bool`
- `get_last_interrupt_reason() -> str`

#### 资源

- `close()`

## `LinglongHSdkClass`（推荐主入口）

职责：

- 继承 `maniSdkClass`
- 增加后台状态线程和调试线程
- 增加插值、CSV 回放、导航

构造参数：

- `ip: str = DEFAULT_ROBOT_IP`
- `port: int = DEFAULT_CMD_PORT`
- `state_port: int = DEFAULT_STATE_PORT`
- `chassis_tcp_on_send: bool = True`
- `auto_state_thread: bool = True`（启动后台状态轮询）
- `state_poll_timeout: float = 0.02`
- `debug: bool = False`（10Hz 打印 `ctrl/sens`）
- `auto_object_udp_thread: bool = True`（与状态线程类似，默认启动物体目标 UDP 接收）
- `object_udp_listen_port: int = DEFAULT_OBJECT_UDP_LISTEN_PORT`（默认 **5005**；设为 **0** 表示不监听，轨迹中含 `com` 时需与发送端一致且 >0）
- `object_udp_listen_ip: str = "0.0.0.0"`
- `object_udp_timeout: float = 0.2`

常用可读成员：

- `ctrl` / `sens`：继承自 `maniSdkClass`
- `nav_target`：导航 TCP 目标地址
- `object_udp_receiver() -> ObjectUdpReceiver | None`：内置目标接收器；端口为 0 或未启用时为 `None`。轨迹回放等场景把返回值传给 `traj_replan.resolve_live627_*` / `trajectory_playback_task` 的 `object_udp` 参数即可

### `LinglongHSdkClass` 主要扩展 API

#### 物体目标 UDP（内置）

- ``object_udp_receiver()``：见上。内置 **``^base T_cam``**（相机在基座下）**暂不对外开放**；默认 translation `[0.1,0,1]`、RPY `[-2.28,0,-1.5708]` rad。离线对齐：``default_object_udp_base_to_camera_extrinsic()``、``apply_camera_to_base_extrinsic()``。

#### 导航

- `configure_navigation_tcp(ip=DEFAULT_NAV_IP, port=DEFAULT_NAV_PORT, timeout_s=5.0, msg_type=DEFAULT_NAV_MSG_TYPE) -> LinglongHSdkClass`
  - `ip` / `port`：导航 TCP 目标（默认 `192.168.1.204:19206`）
  - `timeout_s`：导航请求超时（秒）
  - `msg_type`：导航消息类型（默认 `3051`）
- `send_navigation_simple(target_id, source_id="SELF_POSITION", task_id="") -> tuple[bool, str]`
  - `target_id`：目标点 ID（必填）
  - `source_id`：起点 ID（默认 `SELF_POSITION`）
  - `task_id`：任务 ID，留空则自动生成
  - 返回 `(ok, response_text)`，`ok` 依据响应 `ret_code == 0`

#### 插值

- `send_joint_interpolation(..., interp_start=ManiInterpStartSource.kStatus) -> bool`
- `send_eef_interpolation(..., interp_start=ManiInterpStartSource.kStatus) -> bool`
- `send_interpolation(mode, send_time, **kwargs) -> bool`
  - `mode` 支持 `"joint"` / `"eef"`
  - `interp_start`：``ManiInterpStartSource.kStatus``（默认，从 `sens` 起步）或 ``kCtrl``（从当前 `ctrl` 起步）；亦兼容字符串 ``"status"`` / ``"ctrl"`` 或整型 ``0`` / ``1``
- `reset_to_init(send_time=2.0, mode="eef", interp_start=ManiInterpStartSource.kStatus) -> bool`
  - `send_time`：复位插值时长（秒）
  - `mode`：`"eef"` 或 `"joint"`
  - `interp_start`：同上，传给内部插值
  - `mode="eef"` 自动设置 `ctrl.mode=1`
  - `mode="joint"` 自动设置 `ctrl.mode=0`

#### CSV 回放

- `playback_csv(csv_file, mode=0, value_field="status", loop=False, speed=1.0, print_every=50, send_chassis_tcp=False) -> bool`
  - `csv_file`：CSV 文件路径
  - `mode=0` 关节，`mode=1` 末端
  - `value_field` 选择 CSV 的 `status` 或 `exp` 列
  - `loop=True` 循环播放
  - `speed` 播放倍率（`1.0` 为原速）
  - `print_every` 每 N 帧打印一次日志
  - `send_chassis_tcp=False` 建议保持默认（避免和 `send()` 联动重复发送）

#### 其他

- `fetch_robot_state(timeout=0.05)` / `recv()`：覆写为线程场景下更稳妥行为
- `wait_for_first_state_udp(total_timeout_s=2.0, poll_timeout_s=0.05) -> bool`：有后台状态线程时**不与**后台争用同一 UDP socket，仅在条件变量上等待 `_state_udp_received_ok`；无线程时回退为基类实现
- `close()`：先停目标 UDP、调试/状态线程，再调用父类关闭 socket

## `ObjectUdpReceiver`（目标表；通常由 `LinglongHSdkClass` 启动）

职责与协议与旧版相同：单独线程监听 **UTF-8 JSON**，根结构须为 ``{"objects":[...]}``，元素含 ``name`` / ``camera_pos`` / ``camera_att``；合法包内**所有**对象写入表 ``get_tracked_objects()``，**不**负责业务上选哪个 key（由轨迹模块按 `task_name` 等查表）。

**推荐用法**：使用 ``LinglongHSdkClass(...)`` 默认在 **5005** 拉起接收（可用 ``object_udp_listen_port=0`` 关闭），通过 ``sdk.object_udp_receiver()`` 取实例交给 ``traj_replan``。

仍可**独立构造**本类（例如单测绑定临时端口）；``start`` / ``stop`` 自控生命周期。``BaseToCameraExtrinsic``、``default_object_udp_base_to_camera_extrinsic``、``apply_camera_to_base_extrinsic`` 仍导出，供离线验算；**外参写入暂不开放**。

构造参数：

- `listen_port`、`listen_ip`、`timeout`：同前，默认端口 **5005**

主要方法：`start` / `stop` / `get_tracked_objects()`
---

## 5. 状态更新链路（非常重要）

调用 `fetch_robot_state()` 或后台状态线程单轮执行 `_exchange_state_once()` 时：

1. 向 `stateIpPort` 发送 UDP 状态请求（使用当前 `sens.pack_bytes()`）
2. 接收 UDP 状态包并写入 `sens`；**合法解包成功**后内部置 `_state_udp_received_ok`（供 `wait_for_first_state_udp` / `is_state_udp_received_ok` 使用）
3. 通过底盘状态 TCP 查询速度（消息类型 `1005`）
4. 速度回填：
   - `sens.base_vel`
   - `ctrl.car_translation_status`
   - `ctrl.car_rotation_status`
5. 通过底盘状态 TCP 查询电量（消息类型 `1007`，默认 `{"simple": true}`）
6. 电量回填：
   - `self.battery_level`
   - `sens.battery_level`

含义总结：

- `exp`：你希望机器人执行的目标
- `status`：机器人/底盘实际反馈

---

## 6. 快速使用模板

### 模板 A：基础控制循环

```python
from linglong_h_sdk import LinglongHSdkClass
import time

sdk = LinglongHSdkClass(auto_state_thread=True, chassis_tcp_on_send=True)
try:
    sdk.set_mode(1)  # eef
    sdk.set_left_end([0.3, 0.25, 0.65], [0.0, 0.0, 0.0])
    sdk.set_base_vel(0.0, 0.3)
    for _ in range(100):
        sdk.send()
        time.sleep(0.02)
finally:
    sdk.close()
```

### 模板 B：获取实际速度和电量

```python
sdk.fetch_robot_state()
vx = sdk.ctrl.car_translation_status
w = sdk.ctrl.car_rotation_status
battery = sdk.get_battery_level()
```

### 模板 C：导航请求

```python
sdk.configure_navigation_tcp(ip="192.168.1.204", port=19206)
ok, resp = sdk.send_navigation_simple(target_id="P1")
```

---

## 7. 测试脚本说明（`sdk_test`）

- `test_trajectory_playback_robot.py`
  - 真机轨迹回放示例：顶部 `PlaybackSettings` 修改 IP、任务名、`INTERP_START_RESET` / `INTERP_START_PLAYBACK`（`ManiInterpStartSource`）等
  - **首帧状态 UDP**：仅当 reset 或 playback **至少其一为 `kStatus`**（插值从 `sens` 起步）时调用 `wait_for_first_state_udp`（默认最长等待 `WAIT_FIRST_STATE_UDP_S`，常为 2s）；**均为 `kCtrl`** 则跳过（起点来自 `ctrl`）
  - 构造 SDK 后可设 **`AFTER_FIRST_STATE_SETTLE_S`** 秒静置（默认 `1.0`）
  - 运行：`PYTHONPATH=. python3 sdk_test/test_trajectory_playback_robot.py`
- `test_trajectory_playback_gate_loop.py`
  - 与 C++ **`test_trajectory_playback_gate_loop_cpp`** 对应：YAML **按顺序** UDP 放行；**回放状态 UDP** 仅在 **本条 CSV 结束后、等待下一次放行** 时发送 **刚完成的** action（回放中不发）；`GateLoopSettings` 里 `PLAYBACK_STATUS_*`
  - 运行：`PYTHONPATH=. python3 sdk_test/test_trajectory_playback_gate_loop.py`
  - 放行发包（小端 int32，默认 1）：`python3 sdk_test/send_playback_gate_udp.py --host 127.0.0.1 --port 5033`
  - **验证回放状态 UDP**（监听 `PLAYBACK_STATUS_UDP_*`）：`python3 sdk_test/recv_playback_status_udp.py --bind 0.0.0.0 --port 5000`
- `test_sdk_trajectory_load.py`
  - 离线校验：从仓库根 `config/` 发现并加载任务（如 `eef_demo`），不连机器人
- `test_robot_mode_enable_disable.py`
  - 模式切换 + 动作 + 插值 + CSV 回放综合联调
- `test_chassis_speed_battery.py`
  - 底盘实际 `vx/w` + 电量持续打印
  - 包含 `w=0.5` 持续 1s 后回零流程
- `test_navigation_simple.py`
  - 无 GUI 导航请求（单次）
- `test_sdk_base.py` / `test_sdk_extend.py` / `test_object_udp_receiver.py`
  - 单元测试
- `run_all_tests.py`
  - 发现并运行全部 `test_*.py`

---

## 8. 常见问题与注意事项

- `send()` 报 `mani send paused`
  - 说明你调用过 `pause_mani_send()`，需先 `resume_mani_send()` 或 `send(force=True)`。
- 回放/插值中途停止
  - 常见原因为模式切换、pause 或全局中断序列变化。
- 底盘不动但机械臂在发包
  - 检查 `chassis_tcp_on_send` 是否为 `True`。
- `status` 看起来和 `exp` 不一致
  - 正常，`status` 是实际反馈，存在执行延迟和闭环误差。
- 电量是 `NaN`
  - 说明当前轮询未成功拿到底盘电池响应（19204 链路或响应异常）。

---

## 9. 快速命令

在项目根目录执行：

```bash
python sdk_test/test_chassis_speed_battery.py
python sdk_test/test_navigation_simple.py
python sdk_test/test_robot_mode_enable_disable.py
PYTHONPATH=. python3 sdk_test/test_trajectory_playback_robot.py
PYTHONPATH=. python3 sdk_test/test_trajectory_playback_gate_loop.py
python3 sdk_test/send_playback_gate_udp.py --host 127.0.0.1 --port 5033
python3 sdk_test/recv_playback_status_udp.py --port 5000
python sdk_test/run_all_tests.py
```
