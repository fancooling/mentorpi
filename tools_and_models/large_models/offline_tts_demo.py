#!/usr/bin/env python3
# encoding: utf-8
# @Author: Aiden
# @Date: 2025/02/28
import os
from config import *
from speech import speech
import sherpa_onnx
from pathlib import Path

speech.set_volume(80)
language = 'Chinese'
sherpa_onnx_path = os.path.join(Path.home(), 'third_party/sherpa-onnx')
if language == 'Chinese':
    offline_tts = 'matcha-icefall-zh-baker'
    model_path = f'{sherpa_onnx_path}/{offline_tts}'
    tts = speech.OfflineRealTimeTTS(
        provider="cuda",
        debug=1,
        matcha_acoustic_model=os.path.join(model_path, 'model-steps-3.onnx'),
        matcha_vocoder=os.path.join(sherpa_onnx_path, 'vocos-22khz-univ.onnx'),
        matcha_lexicon=os.path.join(model_path, 'lexicon.txt'),
        matcha_tokens=os.path.join(model_path, 'tokens.txt'),
        tts_rule_fsts=f'{model_path}/phone.fst,{model_path}/date.fst,{model_path}/number.fst',
        sherpa = sherpa_onnx
    )
else:
    offline_tts = 'vits-ljs'
    model_path = f'{sherpa_onnx_path}/{offline_tts}'
    tts = speech.OfflineRealTimeTTS(
        provider="cuda",
        debug=1,
        vits_model=os.path.join(model_path, f'{offline_tts}.onnx'),
        vits_lexicon=os.path.join(model_path, 'lexicon.txt'),
        vits_tokens=os.path.join(model_path, 'tokens.txt'),
        sherpa = sherpa_onnx
    )

# save_path = './resources/audio/offline'
# tts.tts('准备就绪', save_path=os.path.join(save_path, 'start_audio.wav'), play=True)
# tts.tts('我在', save_path=os.path.join(save_path, 'wakeup.wav'), play=True)
# tts.tts('我还在学习中', save_path=os.path.join(save_path, 'error.wav'), play=True)
# tts.tts('小幻没有听清楚，请再说一遍', save_path=os.path.join(save_path, 'no_voice.wav'), play=True)
# tts.tts('我记住了', save_path=os.path.join(save_path, 'record_finish.wav'), play=True)
# tts.tts('开始追踪', save_path=os.path.join(save_path, 'start_track.wav'), play=True)
# tts.tts('获取目标失败', save_path=os.path.join(save_path, 'track_fail.wav'), play=True)
tts.tts('你好，请问有什么可以帮到您', block=True)
# tts(text=None, sid=100, speed=1.0, save_path=None, play=True, block=True, enable_log=False):
