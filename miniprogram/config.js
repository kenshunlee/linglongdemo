const SERVER_PORT = '8765';
const DEFAULT_SERVER_HOST =  '172.18.1.79'; // '192.168.1.132';
const DEFAULT_SERVER_BASE = `http://${DEFAULT_SERVER_HOST}:${SERVER_PORT}`;

function normalizeServerBase(value) {
  const raw = String(value || '').trim().replace(/\/$/, '');
  if (!raw) {
    return DEFAULT_SERVER_BASE;
  }
  if (!raw.startsWith('http://') && !raw.startsWith('https://')) {
    return raw;
  }
  const match = raw.match(/^(https?:\/\/)([^\/?#:]+)(?::\d+)?(.*)$/i);
  if (!match) {
    return raw;
  }
  return `${match[1]}${match[2]}:${SERVER_PORT}${match[3] || ''}`;
}

module.exports = {
  serverPort: SERVER_PORT,
  serverBase: DEFAULT_SERVER_BASE,
  normalizeServerBase,
  serverPresets: [
    {
      key: 'usb',
      label: 'USB 共享网络 IP',
      url: DEFAULT_SERVER_BASE,
    },
    {
      key: 'lan',
      label: '局域网 IP',
      url: DEFAULT_SERVER_BASE,
    },
    {
      key: 'tunnel',
      label: '临时隧道地址',
      url: 'https://example.ngrok-free.app',
    },
  ],
};