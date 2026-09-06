/**
 * MentorPi Fan browser client.
 *
 * The module opens a subscription-only rosbridge connection for factory sensor
 * topics and renders camera/LiDAR/telemetry data. It intentionally implements
 * no rosbridge publish, service-call, action, or parameter operations.
 */
(() => {
  "use strict";

  const RECONNECT_MAX_MS = 10000;
  const CAMERA_RECONNECT_MAX_MS = 10000;
  const SCAN_STALE_MS = 2000;
  const TOPIC_PATTERN = /^\/[A-Za-z0-9_/]+$/;

  const elements = {
    bridgeStatus: document.querySelector("#bridge-status"),
    scanStatus: document.querySelector("#scan-status"),
    cameraStatus: document.querySelector("#camera-status"),
    cameraTopic: document.querySelector("#camera-topic"),
    cameraStart: document.querySelector("#camera-start"),
    cameraStop: document.querySelector("#camera-stop"),
    cameraStream: document.querySelector("#camera-stream"),
    cameraPlaceholder: document.querySelector("#camera-placeholder"),
    lidarTopic: document.querySelector("#lidar-topic"),
    lidarApply: document.querySelector("#lidar-apply"),
    lidarRange: document.querySelector("#lidar-range"),
    lidarCanvas: document.querySelector("#lidar-canvas"),
    scanRate: document.querySelector("#scan-rate"),
    scanPoints: document.querySelector("#scan-points"),
    scanClosest: document.querySelector("#scan-closest"),
    scanAge: document.querySelector("#scan-age"),
    odomX: document.querySelector("#odom-x"),
    odomY: document.querySelector("#odom-y"),
    odomYaw: document.querySelector("#odom-yaw"),
    batteryValue: document.querySelector("#battery-value"),
    bridgeReconnect: document.querySelector("#bridge-reconnect"),
    dashboardUrl: document.querySelector("#dashboard-url"),
    bridgeUrl: document.querySelector("#bridge-url"),
    lastEvent: document.querySelector("#last-event"),
  };

  const state = {
    socket: null,
    reconnectTimer: null,
    reconnectDelayMs: 1000,
    deliberateClose: false,
    lidarTopic: "/scan_raw",
    lastScan: null,
    lastScanAt: 0,
    previousScanAt: 0,
    smoothedRate: 0,
    framePending: false,
    cameraActive: false,
    cameraRetryTimer: null,
    cameraRetryDelayMs: 1000,
    cameraTopic: "",
  };

  const rosbridgeLocation = new URL(window.location.href);
  rosbridgeLocation.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  rosbridgeLocation.port = "9090";
  rosbridgeLocation.pathname = "/";
  rosbridgeLocation.search = "";
  rosbridgeLocation.hash = "";
  const rosBridgeUrl = rosbridgeLocation.toString();
  elements.dashboardUrl.textContent = window.location.href;
  elements.bridgeUrl.textContent = rosBridgeUrl;

  function setStatus(element, text, kind) {
    element.textContent = text;
    element.classList.remove("status-ok", "status-wait", "status-error");
    element.classList.add(`status-${kind}`);
  }

  function recordEvent(message) {
    const timestamp = new Date().toLocaleTimeString();
    elements.lastEvent.textContent = `${timestamp} — ${message}`;
  }

  function validatedTopic(value) {
    const topic = value.trim();
    if (!TOPIC_PATTERN.test(topic) || topic.includes("//")) {
      throw new Error("Topic must start with / and contain only letters, numbers, _ and /.");
    }
    return topic;
  }

  function sendSubscription(id, topic, throttleRate = 0) {
    if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
      return;
    }

    state.socket.send(JSON.stringify({
      op: "subscribe",
      id,
      topic,
      compression: "none",
      throttle_rate: throttleRate,
      queue_length: 1,
    }));
  }

  function sendUnsubscribe(id, topic) {
    if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
      return;
    }
    state.socket.send(JSON.stringify({ op: "unsubscribe", id, topic }));
  }

  function subscribeToFactoryTopics() {
    sendSubscription("fan-lidar", state.lidarTopic, 100);
    sendSubscription("fan-odom", "/odom", 200);
    sendSubscription("fan-battery", "/ros_robot_controller/battery", 1000);
  }

  function scheduleReconnect() {
    if (state.deliberateClose || state.reconnectTimer) {
      return;
    }

    const delay = state.reconnectDelayMs;
    state.reconnectTimer = window.setTimeout(() => {
      state.reconnectTimer = null;
      connectBridge();
    }, delay);
    state.reconnectDelayMs = Math.min(state.reconnectDelayMs * 2, RECONNECT_MAX_MS);
    recordEvent(`ROS bridge reconnect scheduled in ${delay / 1000}s`);
  }

  function connectBridge() {
    if (state.reconnectTimer) {
      window.clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }

    if (state.socket) {
      state.deliberateClose = true;
      state.socket.close();
    }

    state.deliberateClose = false;
    setStatus(elements.bridgeStatus, "ROS bridge: connecting", "wait");
    recordEvent("Connecting to factory rosbridge");

    const socket = new WebSocket(rosBridgeUrl);
    state.socket = socket;

    socket.addEventListener("open", () => {
      if (state.socket !== socket) {
        socket.close();
        return;
      }
      state.reconnectDelayMs = 1000;
      setStatus(elements.bridgeStatus, "ROS bridge: connected", "ok");
      recordEvent("Connected to factory rosbridge");
      subscribeToFactoryTopics();
    });

    socket.addEventListener("message", event => {
      if (state.socket !== socket || typeof event.data !== "string") {
        return;
      }

      let envelope;
      try {
        envelope = JSON.parse(event.data);
      } catch (_error) {
        recordEvent("Ignored a non-JSON rosbridge message");
        return;
      }

      if (envelope.op !== "publish" || !envelope.msg) {
        return;
      }

      if (envelope.topic === state.lidarTopic) {
        handleScan(envelope.msg);
      } else if (envelope.topic === "/odom") {
        handleOdometry(envelope.msg);
      } else if (envelope.topic === "/ros_robot_controller/battery") {
        handleBattery(envelope.msg);
      }
    });

    socket.addEventListener("error", () => {
      if (state.socket === socket) {
        setStatus(elements.bridgeStatus, "ROS bridge: unavailable", "error");
        recordEvent("Factory rosbridge connection failed");
      }
    });

    socket.addEventListener("close", () => {
      if (state.socket !== socket) {
        return;
      }
      state.socket = null;
      setStatus(elements.bridgeStatus, "ROS bridge: disconnected", "error");
      if (!state.deliberateClose) {
        scheduleReconnect();
      }
    });
  }

  function handleScan(scan) {
    if (!Array.isArray(scan.ranges) || !Number.isFinite(scan.angle_min) || !Number.isFinite(scan.angle_increment)) {
      recordEvent("Ignored malformed LaserScan data");
      return;
    }

    const now = performance.now();
    if (state.previousScanAt > 0) {
      const instantaneousRate = 1000 / Math.max(now - state.previousScanAt, 1);
      state.smoothedRate = state.smoothedRate === 0
        ? instantaneousRate
        : state.smoothedRate * 0.8 + instantaneousRate * 0.2;
    }
    state.previousScanAt = now;
    state.lastScanAt = Date.now();
    state.lastScan = scan;
    setStatus(elements.scanStatus, "LiDAR: live", "ok");
    requestScanDraw();
  }

  function handleOdometry(message) {
    const pose = message.pose?.pose;
    const position = pose?.position;
    const orientation = pose?.orientation;
    if (!position || !orientation) {
      return;
    }

    elements.odomX.textContent = `${formatNumber(position.x, 2)} m`;
    elements.odomY.textContent = `${formatNumber(position.y, 2)} m`;

    const siny = 2 * (orientation.w * orientation.z + orientation.x * orientation.y);
    const cosy = 1 - 2 * (orientation.y ** 2 + orientation.z ** 2);
    const yawDegrees = Math.atan2(siny, cosy) * 180 / Math.PI;
    elements.odomYaw.textContent = `${formatNumber(yawDegrees, 1)}°`;
  }

  function handleBattery(message) {
    if (Number.isFinite(message.data)) {
      elements.batteryValue.textContent = String(message.data);
    }
  }

  function formatNumber(value, digits) {
    return Number.isFinite(value) ? value.toFixed(digits) : "--";
  }

  function requestScanDraw() {
    if (state.framePending) {
      return;
    }
    state.framePending = true;
    window.requestAnimationFrame(() => {
      state.framePending = false;
      drawScan();
    });
  }

  function prepareCanvas() {
    const canvas = elements.lidarCanvas;
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(Math.round(rect.width * ratio), 1);
    const height = Math.max(Math.round(rect.height * ratio), 1);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }

    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { context, width: rect.width, height: rect.height };
  }

  function drawGrid(context, width, height, displayRange) {
    const centerX = width / 2;
    const centerY = height / 2;
    const radius = Math.max(Math.min(width, height) / 2 - 30, 20);

    context.clearRect(0, 0, width, height);
    context.strokeStyle = "rgba(112, 170, 174, 0.20)";
    context.fillStyle = "rgba(145, 170, 169, 0.82)";
    context.lineWidth = 1;
    context.font = "11px ui-monospace, monospace";

    for (let ring = 1; ring <= 4; ring += 1) {
      const ringRadius = radius * ring / 4;
      context.beginPath();
      context.arc(centerX, centerY, ringRadius, 0, Math.PI * 2);
      context.stroke();
      context.fillText(`${formatNumber(displayRange * ring / 4, 1)}m`, centerX + 6, centerY - ringRadius + 14);
    }

    context.beginPath();
    context.moveTo(centerX, centerY - radius);
    context.lineTo(centerX, centerY + radius);
    context.moveTo(centerX - radius, centerY);
    context.lineTo(centerX + radius, centerY);
    context.stroke();

    context.fillStyle = "#58f0c2";
    context.beginPath();
    context.moveTo(centerX, centerY - 10);
    context.lineTo(centerX - 7, centerY + 8);
    context.lineTo(centerX + 7, centerY + 8);
    context.closePath();
    context.fill();

    return { centerX, centerY, radius };
  }

  function drawScan() {
    const { context, width, height } = prepareCanvas();
    const scan = state.lastScan;
    const selectedRange = Number(elements.lidarRange.value);
    const reportedMax = Number.isFinite(scan?.range_max) ? scan.range_max : 8;
    const displayRange = selectedRange > 0 ? selectedRange : Math.min(Math.max(reportedMax, 2), 12);
    const grid = drawGrid(context, width, height, displayRange);

    if (!scan) {
      context.fillStyle = "rgba(145, 170, 169, 0.82)";
      context.font = "14px ui-sans-serif, system-ui, sans-serif";
      context.textAlign = "center";
      context.fillText(`Waiting for ${state.lidarTopic}`, grid.centerX, grid.centerY + 38);
      context.textAlign = "start";
      return;
    }

    const scale = grid.radius / displayRange;
    const rangeMin = Number.isFinite(scan.range_min) ? scan.range_min : 0;
    const rangeMax = Number.isFinite(scan.range_max) ? scan.range_max : Number.POSITIVE_INFINITY;
    const stride = Math.max(Math.floor(scan.ranges.length / 900), 1);
    let validPoints = 0;
    let closest = Number.POSITIVE_INFINITY;

    context.fillStyle = "#71c8ff";
    for (let index = 0; index < scan.ranges.length; index += stride) {
      const range = scan.ranges[index];
      if (!Number.isFinite(range) || range < rangeMin || range > rangeMax || range > displayRange) {
        continue;
      }

      const angle = scan.angle_min + index * scan.angle_increment;
      const x = grid.centerX - Math.sin(angle) * range * scale;
      const y = grid.centerY - Math.cos(angle) * range * scale;
      context.fillRect(x - 1.25, y - 1.25, 2.5, 2.5);
      validPoints += 1;
      closest = Math.min(closest, range);
    }

    elements.scanRate.textContent = `${formatNumber(state.smoothedRate, 1)} Hz`;
    elements.scanPoints.textContent = String(validPoints);
    elements.scanClosest.textContent = Number.isFinite(closest) ? `${closest.toFixed(2)} m` : "-- m";
  }

  function clearCameraRetry() {
    if (state.cameraRetryTimer) {
      window.clearTimeout(state.cameraRetryTimer);
      state.cameraRetryTimer = null;
    }
  }

  function scheduleCameraRetry() {
    if (!state.cameraActive || state.cameraRetryTimer) {
      return;
    }

    const delay = state.cameraRetryDelayMs;
    state.cameraRetryTimer = window.setTimeout(() => {
      state.cameraRetryTimer = null;
      openCameraStream();
    }, delay);
    state.cameraRetryDelayMs = Math.min(
      state.cameraRetryDelayMs * 2,
      CAMERA_RECONNECT_MAX_MS,
    );
    recordEvent(`Camera reconnect scheduled in ${delay / 1000}s`);
  }

  function openCameraStream() {
    if (!state.cameraActive) {
      return;
    }

    elements.cameraPlaceholder.classList.add("is-hidden");
    elements.cameraStream.classList.add("is-active");
    const parameters = new URLSearchParams({
      topic: state.cameraTopic,
      type: "mjpeg",
      qos_profile: "sensor_data",
      quality: "85",
      client_id: "mentorpi-fan",
      attempt: String(Date.now()),
    });
    elements.cameraStream.src = `/video/stream?${parameters.toString()}`;
    setStatus(elements.cameraStatus, "Camera: connecting", "wait");
    recordEvent(`Opening factory video stream ${state.cameraTopic}`);
  }

  function startCamera() {
    try {
      state.cameraTopic = validatedTopic(elements.cameraTopic.value);
    } catch (error) {
      setStatus(elements.cameraStatus, "Camera: invalid topic", "error");
      recordEvent(error.message);
      return;
    }

    state.cameraActive = true;
    state.cameraRetryDelayMs = 1000;
    clearCameraRetry();
    openCameraStream();
  }

  function stopCamera() {
    state.cameraActive = false;
    clearCameraRetry();
    elements.cameraStream.removeAttribute("src");
    elements.cameraStream.classList.remove("is-active");
    elements.cameraPlaceholder.classList.remove("is-hidden");
    elements.cameraPlaceholder.textContent = "Camera stream stopped.";
    setStatus(elements.cameraStatus, "Camera: stopped", "wait");
    recordEvent("Camera stream stopped");
  }

  elements.cameraStream.addEventListener("load", () => {
    if (!state.cameraActive) {
      return;
    }
    state.cameraRetryDelayMs = 1000;
    setStatus(elements.cameraStatus, "Camera: live", "ok");
    recordEvent("Factory camera stream is live");
  });

  elements.cameraStream.addEventListener("error", () => {
    if (!state.cameraActive) {
      return;
    }
    setStatus(elements.cameraStatus, "Camera: unavailable", "error");
    elements.cameraPlaceholder.textContent = "Factory video service or camera topic is unavailable.";
    elements.cameraPlaceholder.classList.remove("is-hidden");
    elements.cameraStream.classList.remove("is-active");
    recordEvent("Factory camera stream is unavailable");
    scheduleCameraRetry();
  });

  elements.lidarApply.addEventListener("click", () => {
    let topic;
    try {
      topic = validatedTopic(elements.lidarTopic.value);
    } catch (error) {
      setStatus(elements.scanStatus, "LiDAR: invalid topic", "error");
      recordEvent(error.message);
      return;
    }

    sendUnsubscribe("fan-lidar", state.lidarTopic);
    state.lidarTopic = topic;
    state.lastScan = null;
    state.lastScanAt = 0;
    state.previousScanAt = 0;
    state.smoothedRate = 0;
    sendSubscription("fan-lidar", state.lidarTopic, 100);
    setStatus(elements.scanStatus, "LiDAR: waiting", "wait");
    recordEvent(`Subscribed to ${state.lidarTopic}`);
    requestScanDraw();
  });

  elements.cameraStart.addEventListener("click", startCamera);
  elements.cameraStop.addEventListener("click", stopCamera);
  elements.lidarRange.addEventListener("change", requestScanDraw);
  elements.bridgeReconnect.addEventListener("click", () => {
    state.deliberateClose = false;
    connectBridge();
  });

  window.addEventListener("resize", requestScanDraw);
  window.setInterval(() => {
    if (!state.lastScanAt) {
      elements.scanAge.textContent = "--";
      return;
    }

    const ageMs = Date.now() - state.lastScanAt;
    elements.scanAge.textContent = ageMs < 1000 ? `${ageMs} ms` : `${(ageMs / 1000).toFixed(1)} s`;
    if (ageMs > SCAN_STALE_MS) {
      setStatus(elements.scanStatus, "LiDAR: stale", "error");
    }
  }, 250);

  drawScan();
  connectBridge();
})();
