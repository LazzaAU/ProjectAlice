from dataclasses import dataclass, field


@dataclass
class DialogTemplateIntent:
	name: str
	enabledByDefault: bool
	utterances: list = field(default_factory=list)
	slots: list = field(default_factory=list)

	# TODO remove me
	description: str = ''


	def addUtterance(self, text: str):
		self.utterances.append(text)


	def dump(self) -> dict:
		return {
			'name'            : self.name,
			'enabledByDefault': self.enabledByDefault,
			'utterances'      : self.utterances,
			'slots'           : self.slots
		}
