import io
import time
import wave
from pathlib import Path
from typing import Dict, Optional

import sounddevice as sd
# noinspection PyUnresolvedReferences
from webrtcvad import Vad

from core.ProjectAliceExceptions import PlayBytesStopped
from core.base.model.Manager import Manager
from core.commons import constants
from core.dialog.model.DialogSession import DialogSession
from core.util.model.AliceEvent import AliceEvent
from core.voice.WakewordRecorder import WakewordRecorderState


class AudioManager(Manager):

	SAMPLERATE = 16000
	FRAMES_PER_BUFFER = 320

	LAST_USER_SPEECH = 'var/cache/lastUserpeech_{}_{}.wav'
	SECOND_LAST_USER_SPEECH = 'var/cache/secondLastUserSpeech_{}_{}.wav'

	def __init__(self):
		super().__init__()

		self._stopPlayingFlag: Optional[AliceEvent] = None
		self._playing = False
		self._waves: Dict[str, wave.Wave_write] = dict()
		self._audioInputStream = None

		if not self.ConfigManager.getAliceConfigByName('disableCapture'):
			self._vad = Vad(2)

		self._audioInput = None
		self._audioOutput = None


	def onStart(self):
		super().onStart()

		if not self.ConfigManager.getAliceConfigByName('inputDevice'):
			self.logWarning('Input device not set in config, trying to find default device')
			try:
				self._audioInput = sd.query_devices(kind='input')['name']
			except:
				self.logFatal('Audio input not found, cannot continue')
				return
			self.ConfigManager.updateAliceConfiguration(key='inputDevice', value=self._audioInput)
		else:
			self._audioInput = self.ConfigManager.getAliceConfigByName('inputDevice')

		if not self.ConfigManager.getAliceConfigByName('outputDevice'):
			self.logWarning('Output device not set in config, trying to find default device')
			try:
				self._audioOutput = sd.query_devices(kind='output')['name']
			except:
				self.logFatal('Audio output not found, cannot continue')
				return
			self.ConfigManager.updateAliceConfiguration(key='outputDevice', value=self._audioOutput)
		else:
			self._audioOutput = self.ConfigManager.getAliceConfigByName('outputDevice')

		self.setDefaults()

		self._stopPlayingFlag = self.ThreadManager.newEvent('stopPlaying')
		self.MqttManager.mqttClient.subscribe(constants.TOPIC_AUDIO_FRAME.format(self.ConfigManager.getAliceConfigByName('uuid')))


	def onBooted(self):
		if not self.ConfigManager.getAliceConfigByName('disableCapture'):
			self.ThreadManager.newThread(name='audioPublisher', target=self.publishAudio)


	def setDefaults(self):
		self.logInfo(f'Using **{self._audioInput}** for audio input')
		self.logInfo(f'Using **{self._audioOutput}** for audio output')

		sd.default.device = self._audioInput, self._audioOutput


	def onStop(self):
		super().onStop()
		if self._audioInputStream:
			self._audioInputStream.stop(ignore_errors=True)
			self._audioInputStream.close(ignore_errors=True)
		self.MqttManager.mqttClient.unsubscribe(constants.TOPIC_AUDIO_FRAME.format(self.DeviceManager.getMainDevice().uid))


	def onStartListening(self, session: DialogSession):
		if not self.ConfigManager.getAliceConfigByName('recordAudioAfterWakeword') and self.WakewordRecorder.state != WakewordRecorderState.RECORDING:
			return

		path = Path(self.LAST_USER_SPEECH.format(session.user, session.deviceUid))

		if path.exists():
			path.rename(Path(self.SECOND_LAST_USER_SPEECH.format(session.user, session.deviceUid)))

		waveFile = wave.open(str(path), 'wb')
		waveFile.setsampwidth(2)
		waveFile.setframerate(self.AudioServer.SAMPLERATE)
		waveFile.setnchannels(1)
		self._waves[session.deviceUid] = waveFile


	def onCaptured(self, session: DialogSession):
		wav = self._waves.pop(session.deviceUid, None)
		if not wav:
			return
		wav.close()


	def recordFrame(self, deviceUid: str, frame: bytes):
		if deviceUid not in self._waves:
			return

		self._waves[deviceUid].writeframes(frame)


	def publishAudio(self):
		self.logInfo('Starting audio publisher')
		self._audioInputStream = sd.RawInputStream(
			dtype='int16',
			channels=1,
			samplerate=self.SAMPLERATE,
			blocksize=self.FRAMES_PER_BUFFER,
		)
		self._audioInputStream.start()

		speech = False
		silence = self.SAMPLERATE / self.FRAMES_PER_BUFFER
		speechFrames = 0
		minSpeechFrames = round(silence / 3)

		while True:
			if self.ProjectAlice.shuttingDown:
				break

			try:
				frames = self._audioInputStream.read(frames=self.FRAMES_PER_BUFFER)[0]

				if self._vad.is_speech(frames, self.SAMPLERATE):
					if not speech and speechFrames < minSpeechFrames:
						speechFrames += 1
					elif speechFrames >= minSpeechFrames:
						speech = True
						self.MqttManager.publish(
							topic=constants.TOPIC_VAD_UP.format(self.DeviceManager.getMainDevice().uid),
							payload={
								'siteId': self.DeviceManager.getMainDevice().uid
							})
						silence = self.SAMPLERATE / self.FRAMES_PER_BUFFER
						speechFrames = 0
				else:
					if speech:
						if silence > 0:
							silence -= 1
						else:
							speech = False
							self.MqttManager.publish(
								topic=constants.TOPIC_VAD_DOWN.format(self.DeviceManager.getMainDevice().uid),
								payload={
									'siteId': self.DeviceManager.getMainDevice().uid
								})
					else:
						speechFrames = 0

				self.publishAudioFrames(frames)
			except Exception as e:
				self.logDebug(f'Error publishing frame: {e}')


	def publishAudioFrames(self, frames: bytes):
		with io.BytesIO() as buffer:
			with wave.open(buffer, 'wb') as wav:
				wav.setnchannels(1)
				wav.setsampwidth(2)
				wav.setframerate(self.SAMPLERATE)
				wav.writeframes(frames)

			audioFrames = buffer.getvalue()
			self.MqttManager.publish(topic=constants.TOPIC_AUDIO_FRAME.format(self.DeviceManager.getMainDevice().uid), payload=bytearray(audioFrames))


	def onPlayBytes(self, requestId: str, payload: bytearray, deviceUid: str, sessionId: str = None):
		if deviceUid != self.DeviceManager.getMainDevice().uid or self.ConfigManager.getAliceConfigByName('disableSound'):
			return

		self._playing = True
		with io.BytesIO(payload) as buffer:
			try:
				with wave.open(buffer, 'rb') as wav:
					channels = wav.getnchannels()
					framerate = wav.getframerate()

					def streamCallback(outdata, frameCount, _timeInfo, _status):
						data = wav.readframes(frameCount)
						if len(data) < len(outdata):
							outdata[:len(data)] = data
							outdata[len(data):] = b'\x00' * (len(outdata) - len(data))
							raise sd.CallbackStop
						else:
							outdata[:] = data

					stream = sd.RawOutputStream(
						dtype='int16',
						channels=channels,
						samplerate=framerate,
						callback=streamCallback
					)

					self.logDebug(f'Playing wav stream using **{self._audioOutput}** audio output from device **{self.DeviceManager.getDevice(uid=deviceUid).displayName}** (channels: {channels}, rate: {framerate})')
					stream.start()
					while stream.active:
						if self._stopPlayingFlag.is_set():
							stream.stop()
							stream.close()

							if sessionId:
								self.MqttManager.publish(
									topic=constants.TOPIC_TTS_FINISHED,
									payload={
										'id'       : requestId,
										'sessionId': sessionId,
										'siteId'   : deviceUid
									}
								)
								self.DialogManager.onEndSession(self.DialogManager.getSession(sessionId))

							raise PlayBytesStopped
						time.sleep(0.1)

					stream.stop()
					stream.close()
			except PlayBytesStopped:
				self.logDebug('Playing bytes stopped')
			except Exception as e:
				self.logError(f'Playing wav failed with error: {e}')
			finally:
				self._stopPlayingFlag.clear()
				self._playing = False

		# Session id support is not Hermes protocol official
		self.MqttManager.publish(
			topic=constants.TOPIC_PLAY_BYTES_FINISHED.format(deviceUid),
			payload={
				'id'       : requestId,
				'sessionId': sessionId
			}
		)


	def stopPlaying(self):
		self._stopPlayingFlag.set()


	def updateAudioDevices(self):
		self._audioInput = self.ConfigManager.getAliceConfigByName('inputDevice')
		self._audioOutput = self.ConfigManager.getAliceConfigByName('outputDevice')
		self.setDefaults()


	@property
	def isPlaying(self) -> bool:
		return self._playing
