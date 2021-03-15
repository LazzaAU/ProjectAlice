from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from paho.mqtt.client import MQTTMessage

from core.base.SuperManager import SuperManager
from core.base.model import Intent
from core.commons import constants


@dataclass
class DialogSession:
	deviceUid: str
	sessionId: str = ''
	increaseTimeout: int = 0
	user: str = constants.UNKNOWN_USER
	message: MQTTMessage = None
	intentName: str = ''
	notUnderstood: int = 0
	currentState: str = constants.DEFAULT
	hasEnded: bool = False
	hasStarted: bool = False
	isEnding: bool = False
	inDialog = False
	probabilityThreshold: float = 0.5
	text: str = ''
	input: str = ''
	previousInput: str = ''
	isNotification: bool = False
	slots: dict = field(default_factory=dict)
	slotsAsObjects: dict = field(default_factory=dict)
	customData: dict = field(default_factory=dict)
	payload: dict = field(default_factory=dict)
	intentHistory: list = field(default_factory=list)
	intentFilter: list = field(default_factory=list)
	textOnly: bool = False
	lastWasSoundPlayOnly: bool = False # We don't use request ids for play bytes topic. Both say and playaudio use play bytes, therefor we need to track if the last play bytes was sound only or TTS


	def __post_init__(self):  # NOSONAR
		self.probabilityThreshold = SuperManager.getInstance().configManager.getAliceConfigByName('probabilityThreshold')


	def extend(self, message: MQTTMessage, sessionId: str = None):
		if sessionId:
			self.sessionId = sessionId

		self.addToHistory(self.intentName)

		commonsManager = SuperManager.getInstance().commonsManager
		self.message = message
		self.intentName = message.topic
		self.payload = commonsManager.payload(message)
		self.slots = commonsManager.parseSlots(message)
		self.slotsAsObjects = commonsManager.parseSlotsToObjects(message)
		self.customData = commonsManager.parseCustomData(message)


	def update(self, message: MQTTMessage):
		self.addToHistory(self.intentName)

		commonsManager = SuperManager.getInstance().commonsManager
		self.message = message
		self.intentName = message.topic
		self.payload = commonsManager.payload(message)

		if not isinstance(self.payload, dict):
			return

		self.slots.update(commonsManager.parseSlots(message))
		self.slotsAsObjects.update(commonsManager.parseSlotsToObjects(message))
		self.text = self.payload.get('text', '')
		self.input = self.payload.get('input', '')

		if self.customData:
			self.customData.update(commonsManager.parseCustomData(message))
		else:
			self.customData = dict()


	def slotValue(self, slotName: str, index: int = 0, defaultValue: Any = None) -> Any:
		"""
		This returns the slot master value, not what was heard / captured
		"""
		try:
			return self.slotsAsObjects[slotName][index].value['value']
		except (KeyError, IndexError):
			return defaultValue


	def slotRawValue(self, slotName: str) -> str:
		"""
		This returns the slot raw value, what was really heard / captured, so it can be a synonym for example
		"""
		return self.slots.get(slotName, '')


	def addToHistory(self, intent: Intent):
		if str(intent).startswith('hermes/intent'):
			self.intentHistory.append(intent)


	@property
	def previousIntent(self) -> Optional[str]:
		try:
			return str(self.intentHistory[-1])
		except:
			return None


	@property
	def secondLastIntent(self) -> Optional[str]:
		try:
			return str(self.intentHistory[-2])
		except:
			return None
