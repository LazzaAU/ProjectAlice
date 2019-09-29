import json
import struct

import shutil
import tempfile
from enum import Enum
from pathlib import Path

import paho.mqtt.client as mqtt
from pydub import AudioSegment

from core.base.model.Manager import Manager
from core.commons import commons
from core.dialog.model.DialogSession import DialogSession
from core.voice.model.Wakeword import Wakeword
from core.voice.model.WakewordUploadThread import WakewordUploadThread


class WakewordManagerState(Enum):
	IDLE = 1
	RECORDING = 2
	VALIDATING = 3
	CONFIRMING = 4
	TRIMMING = 5
	FINALIZING = 6


class WakewordManager(Manager):
	NAME = 'WakewordManager'

	RECORD_SECONDS = 2.5
	THRESHOLD = -45.0


	def __init__(self):
		super().__init__(self.NAME)

		self._state     = WakewordManagerState.IDLE

		self._audio     = None
		self._wakeword  = None
		self._threshold = 0
		self._wakewordUploadThreads = list()
		self._sampleRate = self.ConfigManager.getAliceConfigByName('micSampleRate')
		self._channels = self.ConfigManager.getAliceConfigByName('micChannels')


	def onStart(self):
		super().onStart()
		self._threshold = self.THRESHOLD


	def onStop(self):
		super().onStop()

		for thread in self._wakewordUploadThreads:
			if thread.isAlive():
				thread.join(timeout=2)


	def onCaptured(self, session: DialogSession):
		if self.state == WakewordManagerState.RECORDING:
			self.MqttManager.mqttClient.unsubscribe('hermes/audioServer/default/audioFrame')
			self._workAudioFile()


	def newWakeword(self, username: str):
		for i in range(1, 4):
			file = Path('/tmp/{}_raw.wav'.format(i))
			if file.exists():
				file.unlink()

		self._wakeword = Wakeword(username)
		self._sampleRate = self.ConfigManager.getAliceConfigByName('micSampleRate')
		self._channels = self.ConfigManager.getAliceConfigByName('micChannels')


	def addASample(self):
		self.wakeword.newSample()
		self.state = WakewordManagerState.RECORDING
		self.MqttManager.mqttClient.subscribe('hermes/audioServer/default/audioFrame')


	def onAudioFrame(self, message: mqtt.MQTTMessage):
		# @author DasBasti
		# https://gist.github.com/DasBasti/050bf6c3232d4bb54c741a1f057459d3

		try:
			riff, size, fformat = struct.unpack('<4sI4s', message.payload[:12])

			if riff != b'RIFF':
				self._logger.error('[{}] Wakeword capture frame parse error'.format(self.name))
				return

			if fformat != b'WAVE':
				self._logger.error('[{}] Wakeword capture frame wrong format'.format(self.name))
				return

			chunkHeader = message.payload[12:20]
			subChunkId, subChunkSize = struct.unpack('<4sI', chunkHeader)

			if subChunkId == b'fmt ':
				aFormat, channels, samplerate, byterate, blockAlign, bps = struct.unpack('HHIIHH', message.payload[20:36])
				bitrate = (samplerate * channels * bps) / 1024

			record = self.wakeword.getSample()

			if not record._datawritten:
				record.setframerate(samplerate)
				record.setnchannels(channels)
				record.setsampwidth(2)

			chunkOffset = 52
			while chunkOffset < size:
				subChunk2Id, subChunk2Size = struct.unpack('<4sI', message.payload[chunkOffset:chunkOffset + 8])
				chunkOffset += 8
				if subChunk2Id == b'data' and self._state == WakewordManagerState.RECORDING:
					record.writeframes(message.payload[chunkOffset:chunkOffset + subChunk2Size])

				chunkOffset = chunkOffset + subChunk2Size + 8

		except Exception as e:
			self._logger.error('[{}] Error capturing wakeword: {}'.format(self.name, e))


	def _workAudioFile(self):
		self._state = WakewordManagerState.TRIMMING

		self.wakeword.getSample().close()

		filepath = self.wakeword.getSamplePath()
		if not filepath.exists():
			self._logger.error('[{}] Raw wakeword "{}" wasn\'t found'.format(self.name, len(self.wakeword.samples)))
			self._state = WakewordManagerState.IDLE
			return


		sound = AudioSegment.from_file(filepath, format='wav', frame_rate=self._sampleRate)
		startTrim = self.detectLeadingSilence(sound)
		endTrim = self.detectLeadingSilence(sound.reverse())
		duration = len(sound)
		trimmed = sound[startTrim : duration - endTrim]
		reworked = trimmed.set_frame_rate(16000)
		reworked = reworked.set_channels(1)

		reworked.export(Path(tempfile.gettempdir(), '{}.wav'.format(len(self.wakeword.samples))), format='wav')
		self._state = WakewordManagerState.CONFIRMING


	def trimMore(self):
		self._threshold += 2
		self._workAudioFile()


	def trimLess(self):
		self._threshold -= 2
		self._workAudioFile()


	def detectLeadingSilence(self, sound):
		trim = 0
		while sound[trim : trim + 10].dBFS < self._threshold and trim < len(sound):
			trim += 10
		return trim


	def tryCaptureFix(self):
		self._sampleRate /= 2
		self._channels = 1
		self._state = WakewordManagerState.IDLE


	def removeSample(self):
		self._wakeword.samples.pop()


	def isDefaultThreshold(self) -> bool:
		return self._threshold == self.THRESHOLD


	def getLastSampleNumber(self) -> int:
		if self._wakeword and self._wakeword.samples:
			return len(self._wakeword.samples)
		else:
			return 1


	def finalizeWakeword(self):
		self._logger.info('[{}] Finalyzing wakeword'.format(self.name))
		self._state = WakewordManagerState.FINALIZING

		config = {
			'hotword_key'            : self._wakeword.username.lower(),
			'kind'                   : 'personal',
			'dtw_ref'                : 0.22,
			'from_mfcc'              : 1,
			'to_mfcc'                : 13,
			'band_radius'            : 10,
			'shift'                  : 10,
			'window_size'            : 10,
			'sample_rate'            : 16000,
			'frame_length_ms'        : 25.0,
			'frame_shift_ms'         : 10.0,
			'num_mfcc'               : 13,
			'num_mel_bins'           : 13,
			'mel_low_freq'           : 20,
			'cepstral_lifter'        : 22.0,
			'dither'                 : 0.0,
			'window_type'            : 'povey',
			'use_energy'             : False,
			'energy_floor'           : 0.0,
			'raw_energy'             : True,
			'preemphasis_coefficient': 0.97,
			'model_version'          : 1
		}

		path = Path(commons.rootDir(), 'trained/hotwords', self.wakeword.username.lower())

		if path.exists():
			self._logger.warning('[{}] Destination directory for new wakeword already exists, deleting'.format(self.name))
			shutil.rmtree(path)

		path.mkdir()

		(path/'config.json').write_text(json.dumps(config, indent=4))

		for i in range(1, 4):
			shutil.move(Path(tempfile.gettempdir(), '{}.wav'.format(i)), path/'{}.wav'.format(i))

		self._addWakewordToSnips(path)
		self.ThreadManager.newThread(name='SatelliteWakewordUpload', target=self._upload, args=[path, self._wakeword.username], autostart=True)

		self._state = WakewordManagerState.IDLE


	def _addWakewordToSnips(self, path: Path):
		# TODO unhardcode sensitivity
		models: list = self.ConfigManager.getSnipsConfiguration('snips-hotword', 'model', createIfNotExist=True)

		if not isinstance(models, list):
			models = list()

		wakewordName = path.name

		add = True
		copy = models.copy()
		for i, model in enumerate(copy):
			if wakewordName in model:
				models.pop(i)
			elif '/snips_hotword=' in model:
				add = False

		if add:
			models.append(str(Path(commons.rootDir(), 'trained/hotwords/snips_hotword=0.53')))

		models.append('{}=0.52'.format(str(path)))
		self.ConfigManager.updateSnipsConfiguration('snips-hotword', 'model', models, restartSnips=True)

		self._upload(path)


	def uploadToNewDevice(self, uid: str):
		directory = Path(commons.rootDir(), 'trained/hotwords')
		for fiile in directory:
			if (directory/fiile).is_file():
				continue

			self._upload(directory/fiile, uid)


	def _upload(self, path: Path, uid: str = ''):
		wakewordName, zipPath = self._prepareHotword(path)

		l = len(self.DeviceManager.getDevicesByType(deviceType='AliceSatellite', connectedOnly=False)) if uid else 1
		for _ in range(0, l):
			port = 8080 + len(self._wakewordUploadThreads)

			payload = {
				'ip': commons.getLocalIp(),
				'port': port,
				'name': wakewordName
			}

			if uid:
				payload['uid'] = uid

			self.MqttManager.publish(topic='projectalice/devices/alice/newhotword', payload=payload)
			thread = WakewordUploadThread(host=commons.getLocalIp(), zipPath=zipPath, port=port)
			self._wakewordUploadThreads.append(thread)
			thread.start()


	def _prepareHotword(self, path: Path) -> tuple:
		wakewordName = path.name
		zipPath = path.parent / (wakewordName + '.zip')

		self._logger.info('[{}] Cleaning up {}'.format(self.name, wakewordName))
		if zipPath.exists():
			zipPath.unlink()

		self._logger.info('[{}] Packing wakeword {}'.format(self.name, wakewordName))
		shutil.make_archive(base_name=zipPath.with_suffix(''), format='zip', root_dir=str(path))

		return wakewordName, zipPath


	@property
	def state(self) -> WakewordManagerState:
		return self._state


	@state.setter
	def state(self, value: WakewordManagerState):
		self._state = value


	@property
	def wakeword(self) -> Wakeword:
		return self._wakeword