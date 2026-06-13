const app = getApp();
const config = require('../../config');

function toFloat(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

Page({
  data: {
    serverBase: '',
    robotReady: false,
    sdkReason: '',
    busy: false,
    lastMsg: '',

    moveDistance: '0.3',
    moveSpeed: '0.15',
    volumePercent: '70',
    turnAngle: 30,

    taskList: [],
    taskIndex: 0,

    reflowState: null,
    missionStatus: null,
    cameraHealth: null,
    reflowCoordSys: 'slam_local',
    reflowTaskId: '',
    reflowSessionId: '',
    reflowTeamId: '',
    reflowRobotId: '',
    reflowSceneId: '',
    missionState: 'IDLE',
    missionRunning: false,
    ros2Running: false,
    cameraRecordDir: '',
    reflowMediaDir: '',

    camera: 'head',
    cameraPreviewOn: false,
    cameraSrc: '',
    cameraError: '',
    cameraTimer: null,

    eefArm: 'left',
    eefSendTime: '0.45',
    eefX: '',
    eefY: '',
    eefZ: '',
    eefRollDeg: '',
    eefPitchDeg: '',
    eefYawDeg: '',
    eefActualText: '',
    eefStepPos: '0.02',
    eefStepAttDeg: '5',
    eefHoldActive: false,
    eefHoldKey: '',
  },

  onLoad() {
    const base = config.normalizeServerBase(wx.getStorageSync('serverBase') || app.globalData.serverBase);
    this.setData({ serverBase: base });
    this.refreshHealth();
    this.loadTasks();
    this.refreshEefCurrent();

    this._eefHoldActive = false;
    this._eefHoldPressing = false;
    this._eefHoldStartTimer = null;
    this._eefPendingNudge = null;
  },

  onUnload() {
    this.stopCameraPreview();
    this.stopEefHoldNudge();
  },

  sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  },

  req(path, method = 'GET', data = null) {
    const base = this.data.serverBase;
    return new Promise((resolve, reject) => {
      wx.request({
        url: `${base}${path}`,
        method,
        data,
        timeout: 20000,
        success: (res) => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            resolve(res.data || {});
            return;
          }
          reject(new Error(`HTTP ${res.statusCode}`));
        },
        fail: (err) => reject(new Error(err.errMsg || 'request failed')),
      });
    });
  },

  async runAction(title, fn) {
    if (this.data.busy) return;
    this.setData({ busy: true, lastMsg: `${title} 执行中...` });
    try {
      const data = await fn();
      const msg = data?.data?.message || data?.detail || `${title} 已执行`;
      this.setData({ lastMsg: msg });
      wx.showToast({ title: '操作成功', icon: 'success' });
    } catch (e) {
      const msg = `${title} 失败: ${e.message}`;
      this.setData({ lastMsg: msg });
      wx.showToast({ title: '操作失败', icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  async refreshHealth() {
    try {
      const [serviceData, robotData] = await Promise.all([
        this.req('/health', 'GET'),
        this.req('/robot/health', 'GET'),
      ]);
      const service = serviceData?.data || {};
      const robot = robotData?.data || {};
      this.setData({
        robotReady: !!robot.sdk_ready,
        sdkReason: robot.sdk_reason || '',
        reflowState: service?.reflow?.state || null,
        missionStatus: service?.mission || null,
        cameraHealth: robot?.camera || null,
        reflowCoordSys: service?.reflow?.state?.coord_sys || 'slam_local',
        reflowTaskId: service?.reflow?.state?.task_id || '',
        reflowSessionId: service?.reflow?.state?.session_id || '',
        reflowTeamId: service?.reflow?.state?.team_id || '',
        reflowRobotId: service?.reflow?.state?.robot_id || '',
        reflowSceneId: service?.reflow?.state?.scene_id || '',
        missionState: service?.mission?.state || 'IDLE',
        missionRunning: !!service?.mission?.running,
        ros2Running: !!robot?.camera?.ros2?.running,
        cameraRecordDir: robot?.camera?.record_dir || '',
        reflowMediaDir: robot?.camera?.reflow_media_dir || '',
      });
    } catch (e) {
      this.setData({ robotReady: false, sdkReason: `健康检查失败: ${e.message}` });
    }
  },

  async loadTasks() {
    try {
      const data = await this.req('/robot/trajectory/tasks', 'GET');
      const tasks = data?.data?.tasks || [];
      this.setData({ taskList: tasks, taskIndex: 0 });
    } catch (_e) {
      this.setData({ taskList: [] });
    }
  },

  radToDeg(v) {
    return (Number(v || 0) * 180.0) / Math.PI;
  },

  degToRad(v) {
    return (Number(v || 0) * Math.PI) / 180.0;
  },

  async refreshEefCurrent() {
    const arm = this.data.eefArm || 'left';
    try {
      const data = await this.req(`/robot/arm/eef/current?arm=${arm}`, 'GET');
      const d = data?.data || {};
      const target = d?.target_pose || [];
      const actual = d?.actual_pose || [];
      if (target.length >= 6) {
        this.setData({
          eefX: String(target[0].toFixed(3)),
          eefY: String(target[1].toFixed(3)),
          eefZ: String(target[2].toFixed(3)),
          eefRollDeg: String(this.radToDeg(target[3]).toFixed(1)),
          eefPitchDeg: String(this.radToDeg(target[4]).toFixed(1)),
          eefYawDeg: String(this.radToDeg(target[5]).toFixed(1)),
        });
      }
      if (actual.length >= 6) {
        this.setData({
          eefActualText: `实际: x=${actual[0].toFixed(3)}, y=${actual[1].toFixed(3)}, z=${actual[2].toFixed(3)}, rpy=(${this.radToDeg(actual[3]).toFixed(1)}, ${this.radToDeg(actual[4]).toFixed(1)}, ${this.radToDeg(actual[5]).toFixed(1)})°`,
        });
      }
    } catch (_e) {
      // Ignore transient read failures to keep manual page responsive.
    }
  },

  onMoveInput(e) {
    this.setData({ moveDistance: e.detail.value });
  },

  onMoveSpeedInput(e) {
    this.setData({ moveSpeed: e.detail.value });
  },

  onTurnInput(e) {
    this.setData({ turnAngle: Number(e.detail.value || 0) });
  },

  onVolumeInput(e) {
    this.setData({ volumePercent: e.detail.value });
  },

  onEefArmChange(e) {
    const arm = e.currentTarget.dataset.arm;
    if (!arm) return;
    this.setData({ eefArm: arm }, () => this.refreshEefCurrent());
  },

  onEefInput(e) {
    const key = e.currentTarget.dataset.key;
    if (!key) return;
    this.setData({ [key]: e.detail.value });
  },

  buildEefAbsolutePose() {
    const x = toFloat(this.data.eefX, 0.0);
    const y = toFloat(this.data.eefY, 0.0);
    const z = toFloat(this.data.eefZ, 0.0);
    const roll = this.degToRad(toFloat(this.data.eefRollDeg, 0.0));
    const pitch = this.degToRad(toFloat(this.data.eefPitchDeg, 0.0));
    const yaw = this.degToRad(toFloat(this.data.eefYawDeg, 0.0));
    return [x, y, z, roll, pitch, yaw];
  },

  onApplyEefAbsolute() {
    const arm = this.data.eefArm || 'left';
    const sendTime = clamp(toFloat(this.data.eefSendTime, 0.45), 0.1, 2.5);
    const pose = this.buildEefAbsolutePose();
    this.runAction('末端绝对位姿', async () => {
      const resp = await this.req('/robot/arm/eef_adjust', 'POST', {
        arm,
        mode: 'absolute',
        pose,
        send_time_s: sendTime,
      });
      this.refreshEefCurrent();
      return resp;
    });
  },

  onApplyEefDelta() {
    const arm = this.data.eefArm || 'left';
    const sendTime = clamp(toFloat(this.data.eefSendTime, 0.45), 0.1, 2.5);
    const pose = this.buildEefAbsolutePose();
    this.runAction('末端增量位姿', async () => {
      const resp = await this.req('/robot/arm/eef_adjust', 'POST', {
        arm,
        mode: 'delta',
        pose,
        send_time_s: sendTime,
      });
      this.refreshEefCurrent();
      return resp;
    });
  },

  async sendEefDeltaStep(axis, sign, silent = false) {
    if (this.data.busy) return null;

    const arm = this.data.eefArm || 'left';
    const sendTime = clamp(toFloat(this.data.eefSendTime, 0.35), 0.1, 2.5);
    const stepPos = clamp(Math.abs(toFloat(this.data.eefStepPos, 0.02)), 0.001, 0.08);
    const stepAtt = this.degToRad(clamp(Math.abs(toFloat(this.data.eefStepAttDeg, 5)), 0.1, 20));
    const pose = [0, 0, 0, 0, 0, 0];

    if (axis === 'x') pose[0] = sign * stepPos;
    else if (axis === 'y') pose[1] = sign * stepPos;
    else if (axis === 'z') pose[2] = sign * stepPos;
    else if (axis === 'r') pose[3] = sign * stepAtt;
    else if (axis === 'p') pose[4] = sign * stepAtt;
    else if (axis === 'yaw') pose[5] = sign * stepAtt;
    else return null;

    this.setData({ busy: true, lastMsg: silent ? '末端连续微调中...' : '末端步进微调执行中...' });
    try {
      const resp = await this.req('/robot/arm/eef_adjust', 'POST', {
        arm,
        mode: 'delta',
        pose,
        send_time_s: sendTime,
      });
      const msg = resp?.data?.message || resp?.detail || '末端步进已执行';
      this.setData({ lastMsg: msg });
      if (!silent) {
        wx.showToast({ title: '操作成功', icon: 'success' });
      }
      return resp;
    } catch (e) {
      const msg = `末端步进失败: ${e.message}`;
      this.setData({ lastMsg: msg });
      if (!silent) {
        wx.showToast({ title: '操作失败', icon: 'none' });
      }
      throw e;
    } finally {
      this.setData({ busy: false });
    }
  },

  onEefNudge(e) {
    const axis = String(e.currentTarget.dataset.axis || '').toLowerCase();
    const sign = Number(e.currentTarget.dataset.sign || 1) >= 0 ? 1 : -1;
    this.sendEefDeltaStep(axis, sign).then(() => this.refreshEefCurrent());
  },

  async runEefHoldLoop() {
    while (this._eefHoldActive) {
      const cfg = this._eefPendingNudge;
      if (!cfg) break;
      try {
        await this.sendEefDeltaStep(cfg.axis, cfg.sign, true);
      } catch (_e) {
        // Keep loop running unless user releases; transient errors are expected on weak links.
      }
      await this.sleep(120);
    }
  },

  stopEefHoldNudge() {
    this._eefHoldPressing = false;
    if (this._eefHoldStartTimer) {
      clearTimeout(this._eefHoldStartTimer);
      this._eefHoldStartTimer = null;
    }
    const wasActive = this._eefHoldActive;
    this._eefHoldActive = false;
    this._eefPendingNudge = null;
    this.setData({ eefHoldActive: false, eefHoldKey: '' });
    if (wasActive) {
      this.setData({ lastMsg: '末端连续微调已停止' });
      this.refreshEefCurrent();
    }
  },

  onEefNudgeTouchStart(e) {
    const axis = String(e.currentTarget.dataset.axis || '').toLowerCase();
    const sign = Number(e.currentTarget.dataset.sign || 1) >= 0 ? 1 : -1;
    if (!axis) return;

    this._eefHoldPressing = true;
    this._eefPendingNudge = { axis, sign };
    if (this._eefHoldStartTimer) {
      clearTimeout(this._eefHoldStartTimer);
    }

    this._eefHoldStartTimer = setTimeout(() => {
      this._eefHoldStartTimer = null;
      if (!this._eefHoldPressing || !this._eefPendingNudge) {
        return;
      }
      this._eefHoldActive = true;
      this.setData({
        lastMsg: '末端连续微调中，松开按钮即停止...',
        eefHoldActive: true,
        eefHoldKey: `${axis}:${sign}`,
      });
      this.runEefHoldLoop();
    }, 260);
  },

  onEefNudgeTouchEnd() {
    const pending = this._eefPendingNudge;
    const startedHold = this._eefHoldActive;
    this._eefHoldPressing = false;

    if (this._eefHoldStartTimer) {
      clearTimeout(this._eefHoldStartTimer);
      this._eefHoldStartTimer = null;
      if (!startedHold && pending) {
        this.sendEefDeltaStep(pending.axis, pending.sign).then(() => this.refreshEefCurrent());
      }
    }

    if (startedHold) {
      this.stopEefHoldNudge();
    } else {
      this._eefPendingNudge = null;
    }
  },

  onEefNudgeTouchCancel() {
    this.stopEefHoldNudge();
  },

  onEefStopHold() {
    this.stopEefHoldNudge();
  },

  onEefQuickLowerZ() {
    this.sendEefDeltaStep('z', -1).then(() => this.refreshEefCurrent());
  },

  onEefQuickLevelRPY() {
    const arm = this.data.eefArm || 'left';
    const sendTime = clamp(toFloat(this.data.eefSendTime, 0.45), 0.1, 2.5);
    const pose = [
      toFloat(this.data.eefX, 0.0),
      toFloat(this.data.eefY, 0.0),
      toFloat(this.data.eefZ, 0.0),
      0.0,
      0.0,
      0.0,
    ];
    this.runAction('姿态回正', async () => {
      const resp = await this.req('/robot/arm/eef_adjust', 'POST', {
        arm,
        mode: 'absolute',
        pose,
        send_time_s: sendTime,
      });
      this.refreshEefCurrent();
      return resp;
    });
  },

  onTaskChange(e) {
    this.setData({ taskIndex: Number(e.detail.value || 0) });
  },

  onInitRobot() {
    this.runAction('初始化', () => this.req('/robot/init', 'POST', {}));
  },

  onSetVolume() {
    const volume = clamp(toFloat(this.data.volumePercent, 70), 0, 100);
    this.runAction('设置音量', () => this.req('/robot/volume', 'POST', { volume_percent: volume }));
  },

  onForward() {
    const d = Math.abs(toFloat(this.data.moveDistance, 0.2));
    const speed = clamp(Math.abs(toFloat(this.data.moveSpeed, 0.15)), 0.03, 0.5);
    this.runAction('前进', () => this.req('/robot/move', 'POST', { distance_m: d, speed_mps: speed }));
  },

  onBackward() {
    const d = -Math.abs(toFloat(this.data.moveDistance, 0.2));
    const speed = clamp(Math.abs(toFloat(this.data.moveSpeed, 0.15)), 0.03, 0.5);
    this.runAction('后退', () => this.req('/robot/move', 'POST', { distance_m: d, speed_mps: speed }));
  },

  onTurnLeft() {
    const a = Math.abs(Number(this.data.turnAngle || 30));
    this.runAction('左转', () => this.req('/robot/turn', 'POST', { angle_deg: a }));
  },

  onTurnRight() {
    const a = -Math.abs(Number(this.data.turnAngle || 30));
    this.runAction('右转', () => this.req('/robot/turn', 'POST', { angle_deg: a }));
  },

  onLeftGripperOpen() {
    this.runAction('左手张开', () => this.req('/robot/gripper', 'POST', { action: 'open', side: 'left' }));
  },

  onLeftGripperClose() {
    this.runAction('左手闭合', () => this.req('/robot/gripper', 'POST', { action: 'close', side: 'left' }));
  },

  onRightGripperOpen() {
    this.runAction('右手张开', () => this.req('/robot/gripper', 'POST', { action: 'open', side: 'right' }));
  },

  onRightGripperClose() {
    this.runAction('右手闭合', () => this.req('/robot/gripper', 'POST', { action: 'close', side: 'right' }));
  },

  onLeftExtend() {
    this.runAction('左手前伸', () => this.req('/robot/arm/preset', 'POST', { arm: 'left', preset: 'extend' }));
  },

  onLeftRetract() {
    this.runAction('左手收回', () => this.req('/robot/arm/preset', 'POST', { arm: 'left', preset: 'retract' }));
  },

  onRightExtend() {
    this.runAction('右手前伸', () => this.req('/robot/arm/preset', 'POST', { arm: 'right', preset: 'extend' }));
  },

  onRightRetract() {
    this.runAction('右手收回', () => this.req('/robot/arm/preset', 'POST', { arm: 'right', preset: 'retract' }));
  },

  onPlayTask() {
    const tasks = this.data.taskList || [];
    if (!tasks.length) {
      wx.showToast({ title: '未发现可用轨迹任务', icon: 'none' });
      return;
    }
    const idx = Number(this.data.taskIndex || 0);
    const task = tasks[idx] || tasks[0];
    this.runAction('轨迹回放', () => this.req('/robot/trajectory/play', 'POST', { task_name: task }));
  },

  onSelectCamera(e) {
    const cam = e.currentTarget.dataset.camera;
    this.setData({ camera: cam });
    if (this.data.cameraPreviewOn) {
      this.startCameraPreview();
    }
  },

  _decodeResponseText(data) {
    if (!data) return '';
    if (typeof data === 'string') return data;
    if (data instanceof ArrayBuffer) {
      try {
        return String.fromCharCode.apply(null, Array.from(new Uint8Array(data)));
      } catch (_e) {
        return '';
      }
    }
    return '';
  },

  _extractFrameError(res) {
    const status = Number(res?.statusCode || 0);
    if (!status || status < 400) {
      return '';
    }

    let detail = '';
    const text = this._decodeResponseText(res?.data);
    if (text) {
      try {
        const parsed = JSON.parse(text);
        detail = parsed?.detail || parsed?.message || '';
      } catch (_e) {
        detail = text.slice(0, 160);
      }
    }

    if (status === 500 && !detail) {
      detail = '后端相机未就绪（可能缺少 ROS2/cv2 依赖或未配置回退 URL）';
    }
    return `相机取帧失败（HTTP ${status}）${detail ? `: ${detail}` : ''}`;
  },

  _pollCameraFrame() {
    if (!this.data.cameraPreviewOn) {
      return;
    }
    if (this._cameraRequesting) {
      return;
    }

    this._cameraRequesting = true;
    const cam = this.data.camera || 'head';
    const base = this.data.serverBase;
    const url = `${base}/robot/camera/frame?camera=${cam}&ts=${Date.now()}`;

    wx.request({
      url,
      method: 'GET',
      responseType: 'arraybuffer',
      timeout: 7000,
      success: (res) => {
        const errorText = this._extractFrameError(res);
        if (errorText) {
          this.setData({ cameraError: errorText });
          return;
        }

        const ctypeRaw = (res?.header?.['content-type'] || res?.header?.['Content-Type'] || 'image/jpeg').toLowerCase();
        const ctype = ctypeRaw.includes('png') ? 'image/png' : 'image/jpeg';
        try {
          const b64 = wx.arrayBufferToBase64(res.data);
          this.setData({
            cameraSrc: `data:${ctype};base64,${b64}`,
            cameraError: '',
          });
        } catch (_e) {
          this.setData({ cameraError: '相机帧解码失败，请检查后端返回格式' });
        }
      },
      fail: (err) => {
        const msg = (err && err.errMsg) ? err.errMsg : 'network error';
        this.setData({ cameraError: `相机画面拉取失败: ${msg}` });
      },
      complete: () => {
        this._cameraRequesting = false;
        if (!this.data.cameraPreviewOn) {
          return;
        }
        this._cameraTickTimer = setTimeout(() => this._pollCameraFrame(), 300);
      },
    });
  },

  startCameraPreview() {
    this.stopCameraPreview();
    this._cameraRequesting = false;
    this._cameraTickTimer = null;
    this.setData({ cameraPreviewOn: true, cameraError: '', cameraSrc: '' });
    this._pollCameraFrame();
  },

  stopCameraPreview() {
    const timer = this._cameraTickTimer || this.data.cameraTimer;
    if (timer) {
      clearTimeout(timer);
    }
    this._cameraTickTimer = null;
    this._cameraRequesting = false;
    this.setData({ cameraTimer: null, cameraPreviewOn: false });
  },

  onTogglePreview() {
    if (this.data.cameraPreviewOn) {
      this.stopCameraPreview();
      return;
    }
    this.startCameraPreview();
  },

  onCameraError() {
    this.setData({ cameraError: '相机画面显示失败，请检查后端取帧状态与网络连通性' });
  },
});
