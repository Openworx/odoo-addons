# -*- coding: utf-8 -*-
from odoo.tests import BaseCase

from ..utils.schema import (
    ArgumentError,
    SchemaError,
    schema_summary,
    validate_args,
    validate_schema,
)

COMPLEX_SCHEMA = {
    'type': 'object',
    'properties': {
        'model_name': {'type': 'string', 'description': 'Odoo model name'},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        'active': {'type': 'boolean'},
        'domain_field': {
            'type': 'string',
            'enum': ['name', 'email', 'phone'],
        },
        'tags': {
            'type': 'array',
            'items': {'type': 'string'},
            'minItems': 0,
            'maxItems': 5,
        },
        'updates': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer'},
                    'changes': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'field': {'type': 'string'},
                                'value': {'anyOf': [
                                    {'type': 'integer'},
                                    {'type': 'string'},
                                ]},
                            },
                            'required': ['field'],
                        },
                    },
                },
                'required': ['id'],
            },
        },
        'code': {'type': 'string', 'pattern': r'^[A-Z]{2,4}$'},
        'note': {'type': ['string', 'null']},
    },
    'required': ['model_name'],
    'additionalProperties': False,
}


class TestValidateSchema(BaseCase):

    def test_valid_complex_schema_passes(self):
        validate_schema(COMPLEX_SCHEMA)  # must not raise

    def test_array_without_items_fails_with_path(self):
        schema = {
            'type': 'object',
            'properties': {
                'domain': {'type': 'array'},
            },
        }
        with self.assertRaises(SchemaError) as cm:
            validate_schema(schema)
        self.assertIn('properties.domain', str(cm.exception))
        self.assertIn('items', str(cm.exception))

    def test_unknown_type_fails(self):
        schema = {
            'type': 'object',
            'properties': {
                'weird': {'type': 'frobnicate'},
            },
        }
        with self.assertRaises(SchemaError) as cm:
            validate_schema(schema)
        self.assertIn('properties.weird', str(cm.exception))

    def test_required_not_in_properties_fails(self):
        schema = {
            'type': 'object',
            'properties': {
                'name': {'type': 'string'},
            },
            'required': ['missing_prop'],
        }
        with self.assertRaises(SchemaError) as cm:
            validate_schema(schema)
        self.assertIn('missing_prop', str(cm.exception))

    def test_bad_regex_fails(self):
        schema = {
            'type': 'object',
            'properties': {
                'code': {'type': 'string', 'pattern': '(unclosed'},
            },
        }
        with self.assertRaises(SchemaError) as cm:
            validate_schema(schema)
        self.assertIn('properties.code', str(cm.exception))

    def test_enum_not_list_fails(self):
        schema = {
            'type': 'object',
            'properties': {
                'state': {'type': 'string', 'enum': 'draft'},
            },
        }
        with self.assertRaises(SchemaError) as cm:
            validate_schema(schema)
        self.assertIn('properties.state', str(cm.exception))

    def test_nested_error_path(self):
        schema = {
            'type': 'object',
            'properties': {
                'updates': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'changes': {'type': 'array'},
                        },
                    },
                },
            },
        }
        with self.assertRaises(SchemaError) as cm:
            validate_schema(schema)
        self.assertIn(
            'properties.updates.items.properties.changes', str(cm.exception))

    def test_anyof_with_bad_branch_fails(self):
        schema = {
            'type': 'object',
            'properties': {
                'value': {'anyOf': [
                    {'type': 'string'},
                    {'type': 'bogus'},
                ]},
            },
        }
        with self.assertRaises(SchemaError) as cm:
            validate_schema(schema)
        self.assertIn('properties.value.anyOf[1]', str(cm.exception))

    def test_type_list_allowed(self):
        schema = {
            'type': 'object',
            'properties': {
                'note': {'type': ['string', 'null']},
            },
        }
        validate_schema(schema)  # must not raise

    def test_min_greater_than_max_fails(self):
        schema = {
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer', 'minimum': 10, 'maximum': 1},
            },
        }
        with self.assertRaises(SchemaError):
            validate_schema(schema)

    def test_required_not_a_list_fails(self):
        schema = {
            'type': 'object',
            'properties': {'name': {'type': 'string'}},
            'required': 'name',
        }
        with self.assertRaises(SchemaError):
            validate_schema(schema)

    def test_unknown_keys_are_ignored(self):
        schema = {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'some_unknown_key': 'whatever'},
            },
            'some_root_unknown_key': True,
        }
        validate_schema(schema)  # must not raise


class TestValidateArgs(BaseCase):

    def test_required_missing_lists_all_names(self):
        schema = {
            'type': 'object',
            'properties': {
                'a': {'type': 'string'},
                'b': {'type': 'string'},
            },
            'required': ['a', 'b'],
        }
        with self.assertRaises(ArgumentError) as cm:
            validate_args({}, schema)
        message = str(cm.exception)
        self.assertIn('a', message)
        self.assertIn('b', message)

    def test_args_must_be_dict(self):
        schema = {'type': 'object', 'properties': {}}
        with self.assertRaises(ArgumentError):
            validate_args(['not', 'a', 'dict'], schema)

    def test_string_to_integer_coercion(self):
        schema = {
            'type': 'object',
            'properties': {'limit': {'type': 'integer'}},
        }
        result = validate_args({'limit': '42'}, schema)
        self.assertEqual(result['limit'], 42)
        self.assertIsInstance(result['limit'], int)

    def test_string_to_number_coercion(self):
        schema = {
            'type': 'object',
            'properties': {'price': {'type': 'number'}},
        }
        result = validate_args({'price': '3.5'}, schema)
        self.assertEqual(result['price'], 3.5)

    def test_float_with_zero_fraction_to_integer_coercion(self):
        schema = {
            'type': 'object',
            'properties': {'limit': {'type': 'integer'}},
        }
        result = validate_args({'limit': 42.0}, schema)
        self.assertEqual(result['limit'], 42)
        self.assertIsInstance(result['limit'], int)

    def test_float_with_fraction_to_integer_fails(self):
        schema = {
            'type': 'object',
            'properties': {'limit': {'type': 'integer'}},
        }
        with self.assertRaises(ArgumentError):
            validate_args({'limit': 42.5}, schema)

    def test_string_to_boolean_coercion_case_insensitive(self):
        schema = {
            'type': 'object',
            'properties': {'active': {'type': 'boolean'}},
        }
        self.assertTrue(validate_args({'active': 'True'}, schema)['active'])
        self.assertFalse(
            validate_args({'active': 'FALSE'}, schema)['active'])

    def test_none_for_nullable_property_kept(self):
        schema = {
            'type': 'object',
            'properties': {'note': {'type': ['string', 'null']}},
        }
        result = validate_args({'note': None}, schema)
        self.assertIsNone(result['note'])

    def test_none_for_optional_non_nullable_property_dropped(self):
        schema = {
            'type': 'object',
            'properties': {'note': {'type': 'string'}},
        }
        result = validate_args({'note': None}, schema)
        self.assertNotIn('note', result)

    def test_none_for_required_non_nullable_property_errors(self):
        schema = {
            'type': 'object',
            'properties': {'note': {'type': 'string'}},
            'required': ['note'],
        }
        with self.assertRaises(ArgumentError):
            validate_args({'note': None}, schema)

    def test_scalar_wrapped_in_array_when_item_type_matches(self):
        schema = {
            'type': 'object',
            'properties': {
                'tags': {'type': 'array', 'items': {'type': 'string'}},
            },
        }
        result = validate_args({'tags': 'solo'}, schema)
        self.assertEqual(result['tags'], ['solo'])

    def test_scalar_not_wrapped_when_item_type_mismatches(self):
        schema = {
            'type': 'object',
            'properties': {
                'tags': {'type': 'array', 'items': {'type': 'integer'}},
            },
        }
        with self.assertRaises(ArgumentError):
            validate_args({'tags': 'solo'}, schema)

    def test_json_string_parsed_for_object(self):
        schema = {
            'type': 'object',
            'properties': {
                'payload': {
                    'type': 'object',
                    'properties': {'x': {'type': 'integer'}},
                    'required': ['x'],
                },
            },
        }
        result = validate_args({'payload': '{"x": 5}'}, schema)
        self.assertEqual(result['payload'], {'x': 5})

    def test_json_string_parsed_for_array(self):
        schema = {
            'type': 'object',
            'properties': {
                'tags': {'type': 'array', 'items': {'type': 'string'}},
            },
        }
        result = validate_args({'tags': '["a", "b"]'}, schema)
        self.assertEqual(result['tags'], ['a', 'b'])

    def test_invalid_json_string_errors_for_object(self):
        schema = {
            'type': 'object',
            'properties': {
                'payload': {
                    'type': 'object',
                    'properties': {'x': {'type': 'integer'}},
                },
            },
        }
        with self.assertRaises(ArgumentError):
            validate_args({'payload': 'not json'}, schema)

    def test_string_scalar_with_mismatched_item_type_errors(self):
        schema = {
            'type': 'object',
            'properties': {
                'ids': {'type': 'array', 'items': {'type': 'integer'}},
            },
        }
        with self.assertRaises(ArgumentError):
            validate_args({'ids': 'not-an-int-and-not-json'}, schema)

    def test_enum_enforced_after_coercion(self):
        schema = {
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer', 'enum': [10, 20]},
            },
        }
        result = validate_args({'limit': '10'}, schema)
        self.assertEqual(result['limit'], 10)
        with self.assertRaises(ArgumentError):
            validate_args({'limit': '15'}, schema)

    def test_pattern_uses_search_semantics(self):
        schema = {
            'type': 'object',
            'properties': {
                'code': {'type': 'string', 'pattern': 'AB'},
            },
        }
        # 'AB' is found in the middle -> search semantics accept it.
        result = validate_args({'code': 'xxABxx'}, schema)
        self.assertEqual(result['code'], 'xxABxx')

    def test_pattern_no_match_errors(self):
        schema = {
            'type': 'object',
            'properties': {
                'code': {'type': 'string', 'pattern': '^AB$'},
            },
        }
        with self.assertRaises(ArgumentError):
            validate_args({'code': 'xxABxx'}, schema)

    def test_max_length_truncates_silently(self):
        schema = {
            'type': 'object',
            'properties': {
                'title': {'type': 'string', 'maxLength': 5},
            },
        }
        result = validate_args({'title': 'abcdefgh'}, schema)
        self.assertEqual(result['title'], 'abcde')

    def test_min_length_errors(self):
        schema = {
            'type': 'object',
            'properties': {
                'title': {'type': 'string', 'minLength': 3},
            },
        }
        with self.assertRaises(ArgumentError):
            validate_args({'title': 'ab'}, schema)

    def test_min_items_errors(self):
        schema = {
            'type': 'object',
            'properties': {
                'tags': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'minItems': 2,
                },
            },
        }
        with self.assertRaises(ArgumentError):
            validate_args({'tags': ['a']}, schema)

    def test_max_items_errors(self):
        schema = {
            'type': 'object',
            'properties': {
                'tags': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'maxItems': 1,
                },
            },
        }
        with self.assertRaises(ArgumentError):
            validate_args({'tags': ['a', 'b']}, schema)

    def test_minimum_maximum_errors(self):
        schema = {
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10},
            },
        }
        with self.assertRaises(ArgumentError):
            validate_args({'limit': 0}, schema)
        with self.assertRaises(ArgumentError):
            validate_args({'limit': 11}, schema)
        result = validate_args({'limit': 5}, schema)
        self.assertEqual(result['limit'], 5)

    def test_anyof_first_branch_success(self):
        schema = {
            'type': 'object',
            'properties': {
                'value': {'anyOf': [
                    {'type': 'string'},
                    {'type': 'integer'},
                ]},
            },
        }
        result = validate_args({'value': 'hi'}, schema)
        self.assertEqual(result['value'], 'hi')

    def test_anyof_second_branch_success(self):
        schema = {
            'type': 'object',
            'properties': {
                'value': {'anyOf': [
                    {'type': 'string', 'pattern': '^only-letters$'},
                    {'type': 'integer'},
                ]},
            },
        }
        result = validate_args({'value': 7}, schema)
        self.assertEqual(result['value'], 7)

    def test_anyof_all_branches_fail(self):
        schema = {
            'type': 'object',
            'properties': {
                'value': {'anyOf': [
                    {'type': 'string'},
                    {'type': 'boolean'},
                ]},
            },
        }
        with self.assertRaises(ArgumentError):
            validate_args({'value': [1, 2]}, schema)

    def test_additional_properties_dropped_when_false_or_absent(self):
        schema_default = {
            'type': 'object',
            'properties': {'a': {'type': 'string'}},
        }
        result = validate_args({'a': 'x', 'b': 'y'}, schema_default)
        self.assertNotIn('b', result)

        schema_false = dict(schema_default, additionalProperties=False)
        result = validate_args({'a': 'x', 'b': 'y'}, schema_false)
        self.assertNotIn('b', result)

    def test_additional_properties_kept_when_true(self):
        schema = {
            'type': 'object',
            'properties': {'a': {'type': 'string'}},
            'additionalProperties': True,
        }
        result = validate_args({'a': 'x', 'b': 'y'}, schema)
        self.assertEqual(result['b'], 'y')

    def test_nested_path_in_error_message(self):
        schema = {
            'type': 'object',
            'properties': {
                'updates': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'changes': {
                                'type': 'array',
                                'items': {
                                    'type': 'object',
                                    'properties': {
                                        'field': {'type': 'integer'},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
        args = {
            'updates': [
                {'changes': [{'field': 1}, {'field': 'not-an-int'}]},
            ],
        }
        with self.assertRaises(ArgumentError) as cm:
            validate_args(args, schema)
        self.assertIn(
            'updates[0].changes[1].field', str(cm.exception))

    def test_input_dict_not_mutated(self):
        schema = {
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer'},
                'tags': {'type': 'array', 'items': {'type': 'string'}},
            },
        }
        original = {'limit': '5', 'tags': ['a']}
        snapshot = {'limit': '5', 'tags': ['a']}
        result = validate_args(original, schema)
        self.assertEqual(original, snapshot)
        self.assertIsNot(result, original)
        self.assertIsNot(result['tags'], original['tags'])

    def test_result_is_json_serialisable(self):
        import json
        result = validate_args(COMPLEX_SCHEMA_VALID_ARGS, COMPLEX_SCHEMA)
        json.dumps(result)  # must not raise

    def test_full_complex_schema_valid_args(self):
        result = validate_args(COMPLEX_SCHEMA_VALID_ARGS, COMPLEX_SCHEMA)
        self.assertEqual(result['model_name'], 'res.partner')
        self.assertEqual(result['limit'], 10)
        self.assertEqual(result['updates'][0]['changes'][0]['value'], 3)


COMPLEX_SCHEMA_VALID_ARGS = {
    'model_name': 'res.partner',
    'limit': '10',
    'active': 'true',
    'domain_field': 'email',
    'tags': ['a', 'b'],
    'updates': [
        {'id': 1, 'changes': [{'field': 'name', 'value': '3'}]},
    ],
    'code': 'ABCD',
}


class TestSchemaSummary(BaseCase):

    def test_basic_case(self):
        schema = {
            'type': 'object',
            'properties': {
                'model_name': {'type': 'string'},
                'limit': {'type': 'integer'},
            },
            'required': ['model_name'],
        }
        summary = schema_summary(schema)
        self.assertEqual(
            summary, 'model_name: string (required); limit: integer')
