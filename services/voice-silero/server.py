import hashlib
import os
import threading
import wave
from pathlib import Path

import torch
from fastapi import FastAPI
from pydantic import BaseModel, Field

from selection import caller_gender
from verified_phrases import correct_verified_phrases
from russian_numbers import prepare_number_text

torch.set_num_threads(max(1, int(os.getenv('SILERO_THREADS', '4'))))
model = torch.package.PackageImporter('/models/v5_5_ru.pt').load_pickle('tts_models', 'model')
model.to(torch.device('cpu'))
SPEAKERS = ('aidar', 'baya', 'kseniya', 'xenia', 'eugene')
MALE = ('aidar', 'eugene')
FEMALE = ('baya', 'kseniya', 'xenia')
root = Path('/media')
root.mkdir(exist_ok=True)
lock = threading.Lock()
app = FastAPI()


class Speech(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    voice: str | None = None
    caller_name: str = Field(default='', max_length=200)


def choose_speaker(data: Speech) -> str:
    gender = caller_gender(data.caller_name, data.text)
    candidates = FEMALE if gender == 'female' else MALE if gender == 'male' else SPEAKERS
    if data.voice in SPEAKERS:
        return data.voice
    key = hashlib.sha256((data.caller_name + '\0' + data.text).encode()).digest()
    return candidates[int.from_bytes(key[:8], 'big') % len(candidates)]


@app.get('/health')
def health():
    return {'status': 'ok', 'engine': 'silero-russian', 'model': 'v5_5_ru', 'voices': list(SPEAKERS)}


@app.post('/speech')
def speech(data: Speech):
    speaker = choose_speaker(data)
    spoken = prepare_number_text(correct_verified_phrases(data.text))
    key = hashlib.sha256(('silero-v5_5_ru-' + speaker + '\0' + spoken).encode()).hexdigest()
    target = root / (key + '.wav')
    if not target.exists():
        with lock:
            if not target.exists():
                audio = model.apply_tts(text=spoken, speaker=speaker, sample_rate=8000)
                samples = audio.detach().cpu().numpy()
                with wave.open(str(target), 'wb') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(8000)
                    wav.writeframes((samples.clip(-1, 1) * 32767).astype('int16').tobytes())
    return {'key': key, 'sound': '/media/' + key, 'engine': 'silero-v5_5_ru', 'voice': speaker, 'cached': target.exists()}
