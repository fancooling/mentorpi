// Aurora 930 capture subprocess. Uses the bundled Deptrum 1.1.22 public SDK
// Device/Stream API; vendor source and license notices stay in the SDK input.
// Usage: aurora-capture SERIAL. Emits MJPG + big-endian length + JPEG to stdout.
// All diagnostics go to stderr. RGB-only 640x400 NV12 is encoded with the
// TurboJPEG ABI exported by the SDK itself; no ROS or firmware-update APIs.
// The parent enforces read deadlines and terminates/reaps hung SDK processes.
#include <arpa/inet.h>
#include <csignal>
#include <iostream>
#include <stdexcept>
#include <unistd.h>
#include "deptrum/device.h"
#include "deptrum/stream.h"

// Stable TurboJPEG 1.x ABI, as exported by the pinned SDK. TJSAMP_420 is 2.
extern "C" {
void* tjInitCompress();
int tjCompressFromYUVPlanes(void*, const unsigned char**, int, const int*, int,
                           int, unsigned char**, unsigned long*, int, int);
void tjFree(unsigned char*);
int tjDestroy(void*);
}
using namespace deptrum;
using namespace deptrum::stream;
static volatile sig_atomic_t running = 1;
static void stop(int) { running = 0; }
static void require(int result, const char* operation) {
  if (result) throw std::runtime_error(std::string(operation) + ": " + std::to_string(result));
}
static void write_all(int fd, const void* data, size_t size) {
  const char* p = static_cast<const char*>(data);
  while (size) {
    ssize_t n = write(fd, p, size);
    if (n < 0 && errno == EINTR && running) continue;
    if (n <= 0) throw std::runtime_error("frame pipe closed");
    p += n; size -= n;
  }
}
int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "aurora-capture SERIAL: RGB-only 640x400 framed JPEG stream\n";
    return 0;
  }
  if (argc != 2 || !*argv[1]) return 2;
  int output = dup(STDOUT_FILENO);
  if (output < 0 || dup2(STDERR_FILENO, STDOUT_FILENO) < 0) return 2;
  signal(SIGTERM, stop); signal(SIGINT, stop); signal(SIGPIPE, SIG_IGN);
  std::shared_ptr<Device> device;
  Stream* stream = nullptr;
  void* encoder = nullptr;
  int result = 0;
  try {
    std::vector<DeviceInformation> devices;
    auto manager = DeviceManager::GetInstance();
    require(manager->GetDeviceList(devices), "discover");
    for (auto& info : devices) {
      if (info.ir_camera.vid == 0x3251 && info.ir_camera.pid == 0x1930 &&
          info.ir_camera.serial_number == argv[1]) {
        device = manager->CreateDevice(info); break;
      }
    }
    if (!device) throw std::runtime_error("selected Aurora unavailable");
    require(device->Open(), "open");
    std::vector<std::tuple<FrameMode, FrameMode, FrameMode>> modes;
    require(device->GetSupportedFrameMode(modes), "modes");
    bool selected = false;
    for (auto mode : modes) {
      if (std::get<1>(mode) == kRes640x400RgbYuv) {
        require(device->SetMode(std::get<0>(mode), std::get<1>(mode), std::get<2>(mode),
                                kSoftwareDecode, kNoDecode), "set mode");
        selected = true; break;
      }
    }
    if (!selected) throw std::runtime_error("640x400 NV12 unsupported");
    encoder = tjInitCompress();
    if (!encoder) throw std::runtime_error("JPEG encoder unavailable");
    require(device->CreateStream(stream, {kRgb}), "create RGB stream");
    require(stream->Start(), "start");
    std::vector<unsigned char> u(640*400/4), v(u.size());
    while (running) {
      StreamFrames frames;
      require(stream->GetFrames(frames, 2000), "frames");
      for (const auto& frame : frames.frame_ptr) {
        if (!frame || frame->frame_type != kRgbFrame) continue;
        if (!frame->data || frame->cols != 640 || frame->rows != 400 || frame->size != 384000)
          throw std::runtime_error("unexpected RGB frame layout");
        auto raw = static_cast<const unsigned char*>(frame->data.get());
        for (size_t i=0; i<u.size(); ++i) { u[i]=raw[256000+2*i]; v[i]=raw[256001+2*i]; }
        const unsigned char* planes[] = {raw, u.data(), v.data()};
        unsigned char* jpeg = nullptr;
        unsigned long size = 0;
        int encoded = tjCompressFromYUVPlanes(encoder, planes, 640, nullptr, 400,
                                             2, &jpeg, &size, 80, 0);
        if (encoded || !jpeg || size > 2*1024*1024) {
          if (jpeg) tjFree(jpeg);
          throw std::runtime_error("JPEG encoding failed");
        }
        try {
          uint32_t length = htonl(static_cast<uint32_t>(size));
          write_all(output, "MJPG", 4); write_all(output, &length, 4);
          write_all(output, jpeg, size);
        } catch (...) { tjFree(jpeg); throw; }
        tjFree(jpeg);
      }
    }
  } catch (const std::exception& error) {
    std::cerr << error.what() << std::endl; result = 1;
  }
  if (stream) { stream->Stop(); device->DestroyStream(stream); }
  if (device) device->Close();
  if (encoder) tjDestroy(encoder);
  close(output);
  return result;
}
