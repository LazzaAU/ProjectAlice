import inspect
import json
import logging
import typing
from pathlib import Path

import toml

from core.ProjectAliceExceptions import ConfigurationUpdateFailed, VitalConfigMissing
from core.base.SuperManager import SuperManager
from core.base.model.Manager import Manager
from core.commons import constants


class ConfigManager(Manager):

	TEMPLATE_FILE = Path('configTemplate.json')
	CONFIG_FILE = Path('config.json')
	SNIPS_CONF = Path('/etc/snips.toml')

	def __init__(self):
		super().__init__()

		self.configFileExists = False

		self._vitalConfigs = list()
		self._aliceConfigurationCategories = list()

		self._aliceTemplateConfigurations: typing.Dict[str, dict] = self.loadJsonFromFile(self.TEMPLATE_FILE)
		self._aliceConfigurations: typing.Dict[str, typing.Any] = dict()

		self._loadCheckAndUpdateAliceConfigFile()
		self._snipsConfigurations = self.loadSnipsConfigurations()

		self._skillsConfigurations = dict()
		self._skillsTemplateConfigurations: typing.Dict[str, dict] = dict()

		self._pendingAliceConfUpdates = dict()


	def onStart(self):
		super().onStart()
		for conf in self._vitalConfigs:
			if conf not in self._aliceConfigurations or self._aliceConfigurations[conf] == '':
				raise VitalConfigMissing(conf)


	def _loadCheckAndUpdateAliceConfigFile(self):
		self.logInfo('Checking Alice configuration file')

		try:
			aliceConfigs = self.loadJsonFromFile(self.CONFIG_FILE)
		except Exception:
			self.logWarning(f'No {str(self.CONFIG_FILE)} found.')
			aliceConfigs = dict()

		if not aliceConfigs:
			self.logInfo('Creating config file from config template')
			aliceConfigs = {configName: configData['defaultValue'] if 'defaultValue' in configData else configData for configName, configData in self._aliceTemplateConfigurations.items()}
			self.CONFIG_FILE.write_text(json.dumps(aliceConfigs, indent=4, ensure_ascii=False))

		changes = False

		# most important: uuid is always required!
		if not aliceConfigs.get('uuid', None):
			import uuid

			##uuid4: no collission expected until extinction of all life (only on earth though!)
			aliceConfigs['uuid'] = str(uuid.uuid4())
			changes = True

		for setting, definition in self._aliceTemplateConfigurations.items():

			if definition['category'] not in self._aliceConfigurationCategories:
				self._aliceConfigurationCategories.append(definition['category'])

			if setting not in aliceConfigs:
				self.logInfo(f'New configuration found: **{setting}**')
				changes = True
				aliceConfigs[setting] = definition.get('defaultValue', '')
			else:
				if setting == 'supportedLanguages':
					continue

				if definition['dataType'] != 'list' and definition['dataType'] != 'longstring':
					if not isinstance(aliceConfigs[setting], type(definition['defaultValue'])):
						changes = True
						try:
							# First try to cast the setting we have to the new type
							aliceConfigs[setting] = type(definition['defaultValue'])(aliceConfigs[setting])
							self.logWarning(f'Existing configuration type missmatch: **{setting}**, cast variable to template configuration type')
						except Exception:
							# If casting failed let's fall back to the new default value
							self.logWarning(f'Existing configuration type missmatch: **{setting}**, replaced with template configuration')
							aliceConfigs[setting] = definition['defaultValue']
				elif definition['dataType'] == 'list':
					values = definition['values'].values() if isinstance(definition['values'], dict) else definition['values']

					if aliceConfigs[setting] not in values:
						changes = True
						self.logWarning(f'Selected value **{aliceConfigs[setting]}** for setting **{setting}** doesn\'t exist, reverted to default value --{definition["defaultValue"]}--')
						aliceConfigs[setting] = definition['defaultValue']

		# Setting logger level immediately
		if aliceConfigs['advancedDebug'] and not aliceConfigs['debug']:
			aliceConfigs['debug'] = True
			changes = True

		if aliceConfigs['debug']:
			logging.getLogger('ProjectAlice').setLevel(logging.DEBUG)

		temp = aliceConfigs.copy()
		for key in temp:
			if key not in self._aliceTemplateConfigurations:
				self.logInfo(f'Deprecated configuration: **{key}**')
				changes = True
				del aliceConfigs[key]

		if changes:
			self.writeToAliceConfigurationFile(aliceConfigs)
		else:
			self._aliceConfigurations = aliceConfigs


	@staticmethod
	def loadJsonFromFile(jsonFile: Path) -> dict:
		try:
			return json.loads(jsonFile.read_text())
		except:
			# Prevents failing for caller
			raise


	def updateAliceConfiguration(self, key: str, value: typing.Any):
		"""
		Updating a core config is sensitive, if the request comes from a skill.
		First check if the request came from a skill at anytime and if so ask permission
		to the user
		:param key: str
		:param value: str
		:return: None
		"""

		rootSkills = [name.lower() for name in self.SkillManager.NEEDED_SKILLS]
		callers = [inspect.getmodulename(frame[1]).lower() for frame in inspect.stack()]
		if 'aliceskill' in callers:
			skillName = callers[callers.index("aliceskill") + 1]
			if skillName not in rootSkills:
				self._pendingAliceConfUpdates[key] = value
				self.logWarning(f'Skill **{skillName}** is trying to modify a core configuration')

				self.ThreadManager.doLater(
					interval=2,
					func=self.MqttManager.publish,
					kwargs={
						'topic': constants.TOPIC_SKILL_UPDATE_CORE_CONFIG_WARNING,
						'payload': {
							'skill': skillName,
							'key'  : key,
							'value': value
						}
					}
				)
				return

		if key not in self._aliceConfigurations:
			self.logWarning(f'Was asked to update **{key}** but key doesn\'t exist')
			raise ConfigurationUpdateFailed()

		self._aliceConfigurations[key] = value
		self.writeToAliceConfigurationFile()


	def bulkUpdateAliceConfigurations(self):
		if not self._pendingAliceConfUpdates:
			return

		for key, value in self._pendingAliceConfUpdates.items():
			if key not in self._aliceConfigurations:
				self.logWarning(f'Was asked to update **{key}** but key doesn\'t exist')
				continue
			self._aliceConfigurations[key] = value

		self.writeToAliceConfigurationFile()
		self.deletePendingAliceConfigurationUpdates()


	def deletePendingAliceConfigurationUpdates(self):
		self._pendingAliceConfUpdates = dict()


	def updateSkillConfigurationFile(self, skillName: str, key: str, value: typing.Any):
		if skillName not in self._skillsConfigurations:
			self.logWarning(f'Was asked to update **{key}** in skill **{skillName}** but skill doesn\'t exist')
			return

		if key not in self._skillsConfigurations[skillName]:
			self.logWarning(f'Was asked to update **{key}** in skill **{skillName}** but key doesn\'t exist')
			return

		# Cast value to template defined type
		vartype = self._skillsTemplateConfigurations[skillName][key]['dataType']
		if vartype == 'boolean':
			if value.lower() in {'on', 'yes', 'true', 'active'}:
				value = True
			elif value.lower() in {'off', 'no', 'false', 'inactive'}:
				value = False
		elif vartype == 'integer':
			try:
				value = int(value)
			except:
				self.logWarning(f'Value missmatch for config **{key}** in skill **{skillName}**')
				value = 0
		elif vartype == 'float' or vartype == 'range':
			try:
				value = float(value)
				if vartype == 'range' and (value > self._skillsTemplateConfigurations[skillName][key]['max'] or value < self._skillsTemplateConfigurations[skillName][key]['min']):
					value = self._skillsTemplateConfigurations[skillName][key]['defaultValue']
					self.logWarning(f'Value for config **{key}** in skill **{skillName}** is out of bound, reverted to default')
			except:
				self.logWarning(f'Value missmatch for config **{key}** in skill **{skillName}**')
				value = 0
		elif vartype in {'string', 'email', 'password', 'longstring'}:
			try:
				value = str(value)
			except:
				self.logWarning(f'Value missmatch for config **{key}** in skill **{skillName}**')
				value = ''

		skillInstance = self.SkillManager.getSkillInstance(skillName=skillName, silent=True)
		if self._skillsTemplateConfigurations[skillName][key].get('beforeUpdate', None):
			if not skillInstance:
				self.logWarning(f'Needed to execute an action before updating a config value for skill **{skillName}** but the skill is not running')
			else:
				function = self._skillsTemplateConfigurations[skillName][key]['beforeUpdate']
				try:
					func = getattr(skillInstance, function)
				except AttributeError:
					self.logWarning(f'Configuration pre processing method **{function}** for skill **{skillName}** does not exist')
				else:
					try:
						if not func(value):
							self.logWarning(f'Configuration pre processing method **{function}** for skill **{skillName}** returned False, cancel setting update')
							return
					except Exception as e:
						self.logError(f'Configuration pre processing method **{function}** for skill **{skillName}** failed: {e}')

		self._skillsConfigurations[skillName][key] = value
		self._writeToSkillConfigurationFile(skillName, self._skillsConfigurations[skillName])

		if self._skillsTemplateConfigurations[skillName][key].get('onUpdate', None):
			if not skillInstance:
				self.logWarning(f'Needed to execute an action after updating a config value for skill **{skillName}** but the skill is not running')
			else:
				function = self._skillsTemplateConfigurations[skillName][key]['onUpdate']
				try:
					func = getattr(skillInstance, function)
				except AttributeError:
					self.logWarning(f'Configuration post processing method **{function}** for skill **{skillName}** does not exist')
				else:
					try:
						if not func(value):
							self.logWarning(f'Configuration post processing method **{function}** for skill **{skillName}** returned False')
					except Exception as e:
						self.logError(f'Configuration post processing method **{function}** for skill **{skillName}** failed: {e}')


	def writeToAliceConfigurationFile(self, confs: dict = None):
		"""
		Saves the given configuration into config.json
		:param confs: the dict to save
		"""
		confs = confs if confs else self._aliceConfigurations

		sort = dict(sorted(confs.items()))
		self._aliceConfigurations = sort

		try:
			self.CONFIG_FILE.write_text(json.dumps(sort, indent=4, sort_keys=True))
		except Exception:
			raise ConfigurationUpdateFailed()


	def _writeToSkillConfigurationFile(self, skillName: str, confs: dict):
		"""
		Saves the given configuration into config.json of the Skill
		:param skillName: the targeted skill
		:param confs: the dict to save
		"""

		# Don't store "active", "version", "author", "conditions" value in skill config file
		misterProper = ['active', 'version', 'author', 'conditions']
		confsCleaned = {key: value for key, value in confs.items() if key not in misterProper}

		skillConfigFile = Path(self.Commons.rootDir(), 'skills', skillName, 'config.json')
		skillConfigFile.write_text(json.dumps(confsCleaned, indent=4, ensure_ascii=False, sort_keys=True))


	def loadSnipsConfigurations(self) -> dict:
		self.logInfo('Loading Snips configuration file')
		if not self.SNIPS_CONF.exists():
			self.Commons.runRootSystemCommand(['cp', Path(self.Commons.rootDir(), 'system/snips/snips.toml'), self.SNIPS_CONF])

		return toml.load(str(self.SNIPS_CONF))


	def updateSnipsConfiguration(self, parent: str, key: str, value, restartSnips: bool = False, createIfNotExist: bool = False, silent: bool = False):
		"""
		Setting a config in snips.toml
		:param silent: output warnings or not
		:param createIfNotExist: bool create the missing parent key value if not present
		:param parent: Parent key in toml
		:param key: Key in that parent key
		:param value: The value to set
		:param restartSnips: Whether to restart Snips or not after changing the value
		"""

		config = self.getSnipsConfiguration(parent=parent, key=key)
		if config is None:
			if createIfNotExist:
				self._snipsConfigurations.setdefault(parent, dict()).setdefault(key, value)
				self.SNIPS_CONF.write_text(toml.dumps(self._snipsConfigurations))
			elif not silent:
				self.logWarning(f'Tried to set **{parent}/{key}** in snips configuration but key was not found')
		else:
			currentValue = self._snipsConfigurations[parent][key]
			if currentValue != value:
				self._snipsConfigurations[parent][key] = value
				self.SNIPS_CONF.write_text(toml.dumps(self._snipsConfigurations))
			else:
				restartSnips = False

		if restartSnips:
			self.Commons.runRootSystemCommand(['systemctl', 'restart', 'snips-nlu'])


	def getSnipsConfiguration(self, parent: str, key: str, silent: bool = False) -> typing.Optional[str]:
		"""
		Getting a specific configuration from snips.toml
		:param silent: whether to print warning or not
		:param parent: parent key
		:param key: key within parent conf
		:return: config value
		"""

		config = self._snipsConfigurations.get(parent, dict()).get(key, None)
		if config is None and not silent:
			self.logWarning(f'Tried to get **{parent}/{key}** in snips configuration but key was not found')

		return config


	def configAliceExists(self, configName: str) -> bool:
		return configName in self._aliceConfigurations


	def configSkillExists(self, configName: str, skillName: str) -> bool:
		return skillName in self._skillsConfigurations and configName in self._skillsConfigurations[skillName]


	def getAliceConfigByName(self, configName: str) -> typing.Any:
		if configName in self._aliceConfigurations:
			return self._aliceConfigurations[configName]
		else:
			self.logDebug(f'Trying to get config **{configName}** but it does not exist')
			return ''


	def getSkillConfigByName(self, skillName: str, configName: str) -> typing.Any:
		return self._skillsConfigurations.get(skillName, dict()).get(configName, None)


	def getSkillConfigs(self, skillName: str) -> dict:
		return self._skillsConfigurations.get(skillName, dict())


	def getSkillConfigsTemplateByName(self, skillName: str, configName: str) -> typing.Any:
		return self._skillsTemplateConfigurations.get(skillName, dict()).get(configName, None)


	def getSkillConfigsTemplate(self, skillName: str) -> dict:
		return self._skillsTemplateConfigurations.get(skillName, dict())


	def loadCheckAndUpdateSkillConfigurations(self, skillToLoad: str = None):
		skillsConfigurations = dict()

		for skillName, skillInstance in self.SkillManager.activeSkills.items():

			if skillToLoad and skillName != skillToLoad:
				continue

			self.logInfo(f'Checking configuration for skill **{skillName}**')

			skillConfigFile = skillInstance.getResource(str(self.CONFIG_FILE))
			skillConfigTemplate = skillInstance.getResource('config.json.template')
			config = dict()

			if not skillConfigFile.exists() and skillConfigTemplate.exists():
				self._newSkillConfigFile(skillName, skillConfigTemplate)
				config = json.load(skillConfigFile.open())

			elif skillConfigFile.exists() and not skillConfigTemplate.exists():
				self.logInfo(f'- Deprecated config file for skill **{skillName}**, removing')
				skillConfigFile.unlink()
				self._skillsTemplateConfigurations[skillName] = dict()
				skillsConfigurations[skillName] = dict()

			elif skillConfigFile.exists() and skillConfigTemplate.exists():
				config = json.load(skillConfigFile.open())
				configSample = json.load(skillConfigTemplate.open())
				self._skillsTemplateConfigurations[skillName] = configSample

				try:
					changes = False
					for setting, definition in configSample.items():
						if setting not in config:
							self.logInfo(f'- New configuration found for skill **{skillName}**: {setting}')
							changes = True
							config[setting] = definition['defaultValue']

						elif 'defaultValue' in definition and not isinstance(config[setting], type(definition['defaultValue'])):
							changes = True
							try:
								# First try to cast the setting we have to the new type
								config[setting] = type(definition['defaultValue'])(config[setting])
								self.logInfo(f'- Existing configuration type missmatch for skill **{skillName}**: {setting}, cast variable to template configuration type')
							except Exception:
								# If casting failed let's fall back to the new default value
								self.logInfo(f'- Existing configuration type missmatch for skill **{skillName}**: {setting}, replaced with template configuration')
								config[setting] = definition['defaultValue']

					temp = config.copy()
					for k, v in temp.items():
						if k not in configSample:
							self.logInfo(f'- Deprecated configuration for skill **{skillName}**: {k}')
							changes = True
							del config[k]

					if changes:
						self._writeToSkillConfigurationFile(skillName, config)
				except Exception as e:
					self.logWarning(f'- Failed updating existing skill config file for skill **{skillName}**: {e}')
					skillConfigFile.unlink()
					if skillConfigTemplate.exists():
						self._newSkillConfigFile(skillName, skillConfigTemplate)
					else:
						self.logWarning(f'- Cannot create config, template not existing, skipping skill **{skillName}**')

			else:
				self._skillsTemplateConfigurations[skillName] = dict()
				skillsConfigurations[skillName] = dict()

			if config:
				skillsConfigurations[skillName] = config

		if not skillToLoad:
			self._skillsConfigurations = skillsConfigurations.copy()
		else:
			self._skillsConfigurations[skillToLoad] = skillsConfigurations[skillToLoad].copy()


	def _newSkillConfigFile(self, skillName: str, skillConfigTemplate: Path):
		self.logInfo(f'- New config file for skill **{skillName}**, creating from template')

		template = json.load(skillConfigTemplate.open())

		confs = {configName: configData['defaultValue'] if 'defaultValue' in configData else configData for configName, configData in template.items()}
		self._skillsTemplateConfigurations[skillName] = template
		self._skillsConfigurations[skillName] = confs
		self._writeToSkillConfigurationFile(skillName, confs)


	def changeActiveLanguage(self, toLang: str):
		if toLang in self.getAliceConfigByName('supportedLanguages'):
			self.updateAliceConfiguration('activeLanguage', toLang)
			return True
		return False


	def getAliceConfigType(self, confName: str) -> typing.Optional[str]:
		# noinspection PyTypeChecker
		return self._aliceConfigurations.get(confName['dataType'])


	def isAliceConfHidden(self, confName: str) -> bool:
		return confName in self._aliceTemplateConfigurations and \
		       self._aliceTemplateConfigurations.get('display') == 'hidden'


	def getAliceConfUpdatePreProcessing(self, confName: str) -> typing.Optional[str]:
		# Some config need some pre processing to run some checks before saving
		return self._aliceTemplateConfigurations.get(confName, dict()).get('beforeUpdate', None)


	def getAliceConfUpdatePostProcessing(self, confName: str) -> typing.Optional[str]:
		# Some config need some post processing if updated while Alice is running
		return self._aliceTemplateConfigurations.get(confName, dict()).get('onUpdate', None)


	def doConfigUpdatePreProcessing(self, function: str, value: typing.Any) -> bool:
		# Call alice config pre processing functions.
		try:
			func = getattr(self, function)
		except AttributeError:
			self.logWarning(f'Configuration pre processing method **{function}** does not exist')
			return False
		else:
			try:
				return func(value)
			except Exception as e:
				self.logError(f'Configuration pre processing method **{function}** failed: {e}')
				return False


	def doConfigUpdatePostProcessing(self, functions: set):
		# Call alice config post processing functions. This will call methods that are needed after a certain setting was
		# updated while Project Alice was running
		for function in functions:
			try:
				func = getattr(self, function)
			except AttributeError:
				self.logWarning(f'Configuration post processing method **{function}** does not exist')
				continue
			else:
				try:
					func()
				except Exception as e:
					self.logError(f'Configuration post processing method **{function}** failed: {e}')
					continue


	def updateMqttSettings(self):
		self.ConfigManager.updateSnipsConfiguration('snips-common', 'mqtt', f'{self.getAliceConfigByName("mqttHost")}:{self.getAliceConfigByName("mqttPort"):}', False, True)
		self.ConfigManager.updateSnipsConfiguration('snips-common', 'mqtt_username', self.getAliceConfigByName('mqttHost'), False, True)
		self.ConfigManager.updateSnipsConfiguration('snips-common', 'mqtt_password', self.getAliceConfigByName('mqttHost'), False, True)
		self.ConfigManager.updateSnipsConfiguration('snips-common', 'mqtt_tls_cafile', self.getAliceConfigByName('mqttHost'), True, True)
		self.reconnectMqtt()


	def reconnectMqtt(self):
		self.MqttManager.reconnect()


	def reloadASR(self):
		self.ASRManager.onStop()
		self.ASRManager.onStart()


	def reloadTTS(self):
		self.TTSManager.onStop()
		self.TTSManager.onStart()


	def checkNewAdminPinCode(self, pinCode: str) -> bool:
		try:
			pin = int(pinCode)
			if len(str(pin)) != 4:
				raise Exception

			return True
		except:
			self.logWarning('Pin code must be 4 digits')
			return False


	def updateAdminPinCode(self):
		self.UserManager.addUserPinCode('admin', self.getAliceConfigByName('adminPinCode'))


	def enableDisableSound(self):
		if self.getAliceConfigByName('disableSoundAndMic'):
			self.WakewordManager.disableEngine()
			self.AudioServer.onStop()
		else:
			self.WakewordManager.enableEngine()
			self.AudioServer.onStart()


	def restartWakewordEngine(self):
		self.WakewordManager.restartEngine()


	def reloadWakeword(self):
		SuperManager.getInstance().restartManager(manager=self.WakewordManager.name)


	def refreshStoreData(self):
		self.SkillStoreManager.refreshStoreData()


	def injectAsound(self, newSettings: str):
		newSettings = newSettings.replace('\r\n', '\n')
		if self.getAliceConfigByName('asoundConfig') != newSettings:
			tmp = Path('/tmp/asound')
			tmp.write_text(newSettings)
			self.Commons.runRootSystemCommand(['sudo', 'mv', tmp, '/etc/asound.conf'])
			self.Commons.runRootSystemCommand(['sudo', 'alsactl', 'kill', 'rescan'])


	def updateTimezone(self, newTimezone: str):
		result = self.Commons.runRootSystemCommand(['timedatectl', 'set-timezone', newTimezone])
		if result.returncode:
			self.logError('Unsupported timezone format')


	def toggleDebugLogs(self):
		if self.getAliceConfigByName('debug'):
			logging.getLogger('ProjectAlice').setLevel(logging.DEBUG)
		else:
			logging.getLogger('ProjectAlice').setLevel(logging.WARN)


	def getGithubAuth(self) -> tuple:
		username = self.getAliceConfigByName('githubUsername')
		token = self.getAliceConfigByName('githubToken')
		return (username, token) if (username and token) else None


	@property
	def snipsConfigurations(self) -> dict:
		return self._snipsConfigurations


	@property
	def aliceConfigurations(self) -> dict:
		return self._aliceConfigurations


	@property
	def aliceConfigurationCategories(self) -> list:
		return sorted(self._aliceConfigurationCategories)


	@property
	def vitalConfigs(self) -> list:
		return self._vitalConfigs


	@property
	def aliceTemplateConfigurations(self) -> dict:
		return self._aliceTemplateConfigurations
