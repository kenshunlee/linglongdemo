module.exports = {
  serverBase: 'http://192.168.1.132:8765',
  serverPresets: [
    {
      key: 'usb',
      label: 'USB 共享网络 IP',
      url: 'http://192.168.1.132:8765',
    },
    {
      key: 'lan',
      label: '局域网 IP',
      url: 'http://192.168.1.132:8765',
    },
    {
      key: 'tunnel',
      label: '临时隧道地址',
      url: 'https://example.ngrok-free.app',
    },
  ],
};