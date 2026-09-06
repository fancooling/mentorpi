#!/usr/bin/env python3
# encoding: utf-8
# @Author: Aiden
# @Date: 2025/03/05
from config import *
from speech import speech

asr = speech.OfflineRealTimeASR()
print(asr.asr('./resources/audio/test_recording.wav'))
