# -*- coding: utf-8 -*-
# Copyright 2022 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License")
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Any

from api import converters
from api import api_specs
from framework import basehandlers
from framework import permissions
from framework import rediscache
from internals import notifier_helpers
from internals import stage_helpers
from internals.core_models import FeatureEntry, MilestoneSet, Stage
from internals.data_types import CHANGED_FIELDS_LIST_TYPE
from internals.review_models import Gate


class StagesAPI(basehandlers.EntitiesAPIHandler):

  def _create_gate_for_stage(
      self, feature_id: int, stage_id: int, gate_type: int) -> None:
    """Create a Gate entity for the given stage type."""
    gate = Gate(feature_id=feature_id, stage_id=stage_id, gate_type=gate_type,
        state=Gate.PREPARING)
    gate.put()

  def _create_stage(self, feature_id: int, feature_type: int, stage_type: int):
    """Create a new Stage entity."""
    stage = Stage(feature_id=feature_id, stage_type=stage_type)
    stage.put()
    gate_type: int | None = stage_helpers.get_gate_for_stage(
        feature_type, stage.stage_type)
    if gate_type is not None:
        self._create_gate_for_stage(
        stage.feature_id, stage.key.integer_id(), gate_type)
    return stage

  def _update_stage(
      self,
      stage: Stage,
      change_info: dict[str, Any],
      changed_fields: CHANGED_FIELDS_LIST_TYPE,
    ) -> bool:
    """Update stage fields with changes provided."""
    stage_was_updated = False
    ot_action_requested = False
    # Check if valid ID is provided and fetch stage if it exists.

    # Update stage fields.
    for field, field_type in api_specs.STAGE_FIELD_DATA_TYPES:
      if field not in change_info:
        continue
      form_field_name = change_info[field]['form_field_name']
      if form_field_name == 'ot_action_requested':
        ot_action_requested = True
      old_value = getattr(stage, field)
      new_value = change_info[field]['value']
      self.update_field_value(stage, field, field_type, new_value)
      changed_fields.append((form_field_name, old_value, new_value))
      stage_was_updated = True

    # Update milestone fields.
    milestones = stage.milestones
    for field, field_type in api_specs.MILESTONESET_FIELD_DATA_TYPES:
      if field not in change_info:
        continue
      if milestones is None:
        milestones = MilestoneSet()
      form_field_name = change_info[field]['form_field_name']
      old_value = getattr(milestones, field)
      new_value = change_info[field]['value']
      self.update_field_value(milestones, field, field_type, new_value)
      changed_fields.append((form_field_name, old_value, new_value))
      stage_was_updated = True
    stage.milestones = milestones

    if stage_was_updated:
      stage.put()

    # Notify of OT request if one was sent.
    if ot_action_requested:
      notifier_helpers.send_ot_creation_notification(stage)

    return stage_was_updated

  def do_get(self, **kwargs):
    """Return a specified stage based on the given ID."""
    stage_id = kwargs.get('stage_id', None)

    if stage_id is None or stage_id == 0:
      self.abort(404, msg='No stage specified.')

    stage: Stage | None = Stage.get_by_id(stage_id)
    if stage is None:
      self.abort(404, msg=f'Stage {stage_id} not found')

    stage_dict = converters.stage_to_json_dict(stage)
    # Add extensions associated with the stage if they exist.
    extensions = stage_helpers.get_ot_stage_extensions(stage_dict['id'])
    if extensions:
      stage_dict['extensions'] = extensions

    return stage_dict

  def do_post(self, **kwargs):
    """Create a new stage."""
    feature_id = kwargs['feature_id']

    feature: FeatureEntry | None = FeatureEntry.get_by_id(feature_id)
    if feature is None:
      self.abort(404, msg=f'Feature {feature_id} not found')

    # Validate the user has edit permissions and redirect if needed.
    redirect_resp = permissions.validate_feature_edit_permission(
        self, feature_id)
    if redirect_resp:
      return redirect_resp

    body = self.get_json_param_dict()
    if 'stage_type' not in body:
      self.abort(404, msg='Stage type not specified.')
    stage_type = int(body['stage_type'])
    # Add the specified field values to the stage. Create a gate if needed.
    stage = self._create_stage(feature_id, feature.feature_type, stage_type)

    # Changing stage values means the cached feature should be invalidated.
    lookup_key = FeatureEntry.feature_cache_key(
        FeatureEntry.DEFAULT_CACHE_KEY, feature_id)
    rediscache.delete(lookup_key)

    # Return  the newly created stage ID.
    return {'message': 'Stage created.', 'stage_id': stage.key.integer_id()}

  def do_patch(self, **kwargs):
    """Update an existing stage based on the stage ID."""
    stage_id = kwargs.get('stage_id', None)

    if stage_id is None:
      self.abort(404, msg='No stage specified.')

    stage: Stage | None = Stage.get_by_id(stage_id)
    if stage is None:
      self.abort(404, msg=f'Stage {stage_id} not found')

    feature: FeatureEntry | None = FeatureEntry.get_by_id(stage.feature_id)
    if feature is None:
      self.abort(404, msg=(f'Feature {stage.feature_id} not found '
                           f'associated with stage {stage_id}'))
    feature_id = feature.key.integer_id()
    body = self.get_json_param_dict()

    user = self.get_current_user()
    is_ot_request = body.get('ot_action_requested', False)
    # If submitting an OT creation request, the user must have feature edit
    # access or be a Chromium/Google account.
    if not user or not is_ot_request or not (
          user.email().endswith('@chromium.org') or
          user.email().endswith('@google.com')):
      # Validate the user has edit permissions and redirect if needed.
      redirect_resp = permissions.validate_feature_edit_permission(
          self, feature_id)
      if redirect_resp:
        return redirect_resp

    changed_fields: CHANGED_FIELDS_LIST_TYPE = []
    # Update specified fields.
    self._update_stage(stage, body, changed_fields)

    notifier_helpers.notify_subscribers_and_save_amendments(
        feature, changed_fields, notify=True)
    # Changing stage values means the cached feature should be invalidated.
    lookup_key = FeatureEntry.feature_cache_key(
        FeatureEntry.DEFAULT_CACHE_KEY, feature_id)
    rediscache.delete(lookup_key)

    return {'message': 'Stage values updated.'}

  def do_delete(self, **kwargs):
    """Delete an existing stage."""
    stage_id = kwargs.get('stage_id', None)
    if stage_id is None:
      self.abort(404, msg='No stage specified.')

    stage: Stage | None = Stage.get_by_id(stage_id)
    if stage is None:
      self.abort(404, msg=f'Stage {stage_id} not found.')

    # Validate the user has edit permissions and redirect if needed.
    redirect_resp = permissions.validate_feature_edit_permission(
        self, stage.feature_id)
    if redirect_resp:
      return redirect_resp

    stage.archived = True
    stage.put()

    return {'message': 'Stage archived.'}
