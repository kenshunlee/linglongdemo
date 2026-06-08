const app = getApp();

Page({
  data: {
    serverBase: '',
    robotReady: false,
    sdkReason: '',
    busy: false,
    lastMsg: '',

    moveDistance: 0.3,
    turnAngle: 30,

    taskList: [],
    taskIndex: 0,

    camera: 'head',
    cameraPreviewOn: false,
    cameraSrc: '',
    cameraError: '',
    cameraTimer: null,
  },

  onLoad() {
    const base = wx.getStorageSync('serverBase') || app.globalData.serverBase;
    this.setData({ serverBase: (base || '').replace(/\/$/, '') });
    this.refreshHealth();
    this.loadTasks();
  },

  onUnload() {
    this.stopCameraPreview();
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
      const data = await this.req('/robot/health', 'GET');
      const d = data?.data || {};
      this.setData({
        robotReady: !!d.sdk_ready,
        sdkReason: d.sdk_reason || '',
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

  onMoveInput(e) {
    this.setData({ moveDistance: Number(e.detail.value || 0) });
  },

  onTurnInput(e) {
    this.setData({ turnAngle: Number(e.detail.value || 0) });
  },

  onTaskChange(e) {
    this.setData({ taskIndex: Number(e.detail.value || 0) });
  },

  onInitRobot() {
    this.runAction('初始化', () => this.req('/robot/init', 'POST', {}));
  },

  onForward() {
    const d = Math.abs(Number(this.data.moveDistance || 0.2));
    this.runAction('前进', () => this.req('/robot/move', 'POST', { distance_m: d }));
  },

  onBackward() {
    const d = -Math.abs(Number(this.data.moveDistance || 0.2));
    this.runAction('后退', () => this.req('/robot/move', 'POST', { distance_m: d }));
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

  startCameraPreview() {
    this.stopCameraPreview();
    this.setData({ cameraPreviewOn: true, cameraError: '' });

    const tick = () => {
      const src = `${this.data.serverBase}/robot/camera/frame?camera=${this.data.camera}&ts=${Date.now()}`;
      this.setData({ cameraSrc: src });
    };

    tick();
    const timer = setInterval(tick, 700);
    this.setData({ cameraTimer: timer });
  },

  stopCameraPreview() {
    const timer = this.data.cameraTimer;
    if (timer) {
      clearInterval(timer);
    }
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
    this.setData({ cameraError: '相机画面拉取失败，请检查后端环境变量中的相机 URL 配置' });
  },
});
