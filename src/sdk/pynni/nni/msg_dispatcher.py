# Copyright (c) Microsoft Corporation. All rights reserved.
#
# MIT License
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
# associated documentation files (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge, publish, distribute,
# sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED *AS IS*, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
# NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
# OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
# ==================================================================================================

import logging
from collections import defaultdict
import json_tricks
import threading

from .protocol import CommandType, send
from .msg_dispatcher_base import MsgDispatcherBase
from .assessor import AssessResult

_logger = logging.getLogger(__name__)

# Assessor global variables
_trial_history = defaultdict(dict)
'''key: trial job ID; value: intermediate results, mapping from sequence number to data'''

_ended_trials = set()
'''trial_job_id of all ended trials.
We need this because NNI manager may send metrics after reporting a trial ended.
TODO: move this logic to NNI manager
'''

def _sort_history(history):
    ret = [ ]
    for i, _ in enumerate(history):
        if i in history:
            ret.append(history[i])
        else:
            break
    return ret

# Tuner global variables
_next_parameter_id = 0
_trial_params = {}
'''key: trial job ID; value: parameters'''
_customized_parameter_ids = set()

def _create_parameter_id():
    global _next_parameter_id  # pylint: disable=global-statement
    _next_parameter_id += 1
    return _next_parameter_id - 1

def _pack_parameter(parameter_id, params, customized=False):
    _trial_params[parameter_id] = params
    ret = {
        'parameter_id': parameter_id,
        'parameter_source': 'customized' if customized else 'algorithm',
        'parameters': params
    }
    return json_tricks.dumps(ret)

class MsgDispatcher(MsgDispatcherBase):
    def __init__(self, tuner, assessor=None):
        super().__init__()
        self.tuner = tuner
        self.assessor = assessor
        if assessor is None:
            _logger.debug('Assessor is not configured')

    def load_checkpoint(self):
        self.tuner.load_checkpoint()
        if self.assessor is not None:
            self.assessor.load_checkpoint()

    def save_checkpoint(self):
        self.tuner.save_checkpoint()
        if self.assessor is not None:
            self.assessor.save_checkpoint()

    def handle_initialize(self, data):
        '''
        data is search space
        '''
        self.tuner.update_search_space(data)
        send(CommandType.Initialized, '')
        return True

    def handle_request_trial_jobs(self, data):
        # data: number or trial jobs
        ids = [_create_parameter_id() for _ in range(data)]
        params_list = self.tuner.generate_multiple_parameters(ids)

        # when parameters is None.
        if len(params_list) == 0:
            send(CommandType.NoMoreTrialJobs, _pack_parameter(ids[0], ''))
        else:
            for i, _ in enumerate(ids):
                send(CommandType.NewTrialJob, _pack_parameter(ids[i], params_list[i]))
        return True

    def handle_update_search_space(self, data):
        self.tuner.update_search_space(data)
        return True

    def handle_add_customized_trial(self, data):
         # data: parameters
        id_ = _create_parameter_id()
        _customized_parameter_ids.add(id_)
        send(CommandType.NewTrialJob, _pack_parameter(id_, data, customized=True))
        return True

    def handle_report_metric_data(self, data):
        """
        :param data: a dict received from nni_manager, which contains:
                    - 'parameter_id': id of the trial
                    - 'value': metric value reported by nni.report_final_result()
                    - 'type': report type, support {'FINAL', 'PERIODICAL'}
        """
        if data['type'] == 'FINAL':
            id_ = data['parameter_id']
            value = data['value']
            if id_ in _customized_parameter_ids:
                self.tuner.receive_customized_trial_result(id_, _trial_params[id_], value)
            else:
                self.tuner.receive_trial_result(id_, _trial_params[id_], value)
        elif data['type'] == 'PERIODICAL':
            if self.assessor is not None:
                self._handle_intermediate_metric_data(data)
            else:
                pass
        else:
            raise ValueError('Data type not supported: {}'.format(data['type']))

        return True

    def handle_trial_end(self, data):
        trial_job_id = data['trial_job_id']
        _ended_trials.add(trial_job_id)
        if trial_job_id in _trial_history:
            _trial_history.pop(trial_job_id)
            if self.assessor is not None:
                self.assessor.trial_end(trial_job_id, data['event'] == 'SUCCEEDED')
        return True

    def _handle_intermediate_metric_data(self, data):
        if data['type'] != 'PERIODICAL':
            return True
        if self.assessor is None:
            return True

        trial_job_id = data['trial_job_id']
        if trial_job_id in _ended_trials:
            return True

        history = _trial_history[trial_job_id]
        history[data['sequence']] = data['value']
        ordered_history = _sort_history(history)
        if len(ordered_history) < data['sequence']:  # no user-visible update since last time
            return True

        try:
            result = self.assessor.assess_trial(trial_job_id, ordered_history)
        except Exception as e:
            _logger.exception('Assessor error')

        if isinstance(result, bool):
            result = AssessResult.Good if result else AssessResult.Bad
        elif not isinstance(result, AssessResult):
            msg = 'Result of Assessor.assess_trial must be an object of AssessResult, not %s'
            raise RuntimeError(msg % type(result))

        if result is AssessResult.Bad:
            _logger.debug('BAD, kill %s', trial_job_id)
            send(CommandType.KillTrialJob, json_tricks.dumps(trial_job_id))
        else:
            _logger.debug('GOOD')
