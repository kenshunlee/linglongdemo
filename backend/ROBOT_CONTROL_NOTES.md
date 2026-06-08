# 机器人手动操控功能记录

本次在不影响语音转写逻辑的前提下新增了独立机器人控制能力。

## 新增文件

- `backend/robot_control.py`
- `miniprogram/pages/manual/*`

## 后端新增接口

- `GET /robot/health`：SDK 可用性、配置与相机配置检查
- `POST /robot/init`：上使能 + 自主模式 + reset_to_init
- `POST /robot/move`：按距离前进/后退
- `POST /robot/turn`：按角度左右转
- `POST /robot/gripper`：夹爪开合
- `POST /robot/arm/preset`：左右手前伸/收回预置动作
- `GET /robot/trajectory/tasks`：发现可回放任务名
- `POST /robot/trajectory/play`：按任务名回放轨迹
- `GET /robot/camera/frame?camera=head|left|right`：抓取相机帧（轮询近实时）

## 环境变量说明

- `ROBOT_SDK_ENABLED`：默认 `1`，设为 `0` 时关闭机器人 SDK
- `ROBOT_IP`、`ROBOT_CMD_PORT`、`ROBOT_STATE_PORT`、`ROBOT_MODE_PORT`
- `ROBOT_CONFIG_ROOT`：轨迹任务目录（默认使用 `灵龙H_API说明/sdk_v1.0/sdk/config`）
- `HEAD_CAMERA_URL`、`LEFT_HAND_CAMERA_URL`、`RIGHT_HAND_CAMERA_URL`

## 说明

- 动作实现基于灵龙 H SDK 现有能力（`RobotModeManager`、`LinglongHSdkClass`、`traj_replan`）。
- 相机实时画面采用后端代理 + 小程序端定时刷新，适配性更高；若现场是 RTSP，可先在边缘端转成可 HTTP 拉取的 JPEG/快照 URL。
- 手臂动作使用近似预置位姿，可根据现场标定数据继续微调。
