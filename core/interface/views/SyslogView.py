import json
import logging

from flask import render_template

from core.interface.model.View import View
from core.util.model.MqttLoggingHandler import MqttLoggingHandler


class SyslogView(View):
	route_base = '/syslog/'

	def index(self):
		super().index()
		logger = logging.getLogger('ProjectAlice')
		history = list()
		for handler in logger.handlers:
			if not isinstance(handler, MqttLoggingHandler):
				continue
			history = handler.history
			break

		return render_template(template_name_or_list='syslog.html',
		                       history=json.dumps(history, ensure_ascii=False),
		                       **self._everyPagesRenderValues)
