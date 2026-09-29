# -*- coding: utf-8 -*-
import json

from odoo.tests import BaseCase

from ..engine.types import (
    assistant_message,
    inline_data_part,
    text_part,
    tool_call_part,
    tool_result_part,
    user_message,
)
from ..provider.errors import ProviderError
from ..provider.mapping import (
    extract_usage,
    from_openai_embeddings,
    from_openai_response,
    to_openai_messages,
    to_openai_response_format,
    to_openai_tools,
)
from ..provider.transport import FakeTransport


class TestToOpenaiMessages(BaseCase):

    def test_instructions_become_leading_system_message(self):
        result = to_openai_messages('Be helpful.', [])
        self.assertEqual(result, [{'role': 'system', 'content': 'Be helpful.'}])

    def test_no_instructions_no_system_message(self):
        result = to_openai_messages(None, [user_message(text_part('hi'))])
        self.assertEqual(result[0]['role'], 'user')

    def test_empty_instructions_no_system_message(self):
        result = to_openai_messages('', [user_message(text_part('hi'))])
        self.assertEqual(result[0]['role'], 'user')

    def test_single_text_part_becomes_plain_string_content(self):
        result = to_openai_messages(None, [user_message(text_part('hello'))])
        self.assertEqual(result, [{'role': 'user', 'content': 'hello'}])

    def test_multiple_text_parts_stay_as_list(self):
        result = to_openai_messages(None, [user_message(text_part('a'), text_part('b'))])
        self.assertEqual(result, [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'a'},
            {'type': 'text', 'text': 'b'},
        ]}])

    def test_image_inline_data_becomes_data_url(self):
        part = inline_data_part('image/png', 'QUJD')
        result = to_openai_messages(None, [user_message(text_part('look'), part)])
        self.assertEqual(result, [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'look'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,QUJD'}},
        ]}])

    def test_pdf_inline_data_becomes_file_part(self):
        part = inline_data_part('application/pdf', 'UERG', filename='invoice.pdf')
        result = to_openai_messages(None, [user_message(part)])
        self.assertEqual(result, [{'role': 'user', 'content': [
            {'type': 'file', 'file': {'filename': 'invoice.pdf', 'file_data': 'data:application/pdf;base64,UERG'}},
        ]}])

    def test_pdf_inline_data_default_filename(self):
        part = inline_data_part('application/pdf', 'UERG')
        result = to_openai_messages(None, [user_message(part)])
        self.assertEqual(result[0]['content'][0]['file']['filename'], 'document.pdf')

    def test_unknown_mimetype_becomes_omitted_text(self):
        part = inline_data_part('application/zip', 'WklQ', filename='archive.zip')
        result = to_openai_messages(None, [user_message(part)])
        self.assertEqual(result, [{
            'role': 'user',
            'content': '[Attachment archive.zip of type application/zip omitted]',
        }])

    def test_tool_result_becomes_separate_tool_message(self):
        result_part = tool_result_part('search', 'call_1', [text_part('found it')])
        result = to_openai_messages(None, [user_message(result_part)])
        self.assertEqual(result, [
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'found it'},
        ])

    def test_tool_results_before_remaining_user_text_same_message(self):
        result_part = tool_result_part('search', 'call_1', [text_part('found it')])
        result = to_openai_messages(None, [user_message(result_part, text_part('thanks'))])
        self.assertEqual(result, [
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'found it'},
            {'role': 'user', 'content': 'thanks'},
        ])

    def test_multiple_tool_results_kept_in_call_order(self):
        p1 = tool_result_part('a', 'call_1', [text_part('one')])
        p2 = tool_result_part('b', 'call_2', [text_part('two')])
        result = to_openai_messages(None, [user_message(p1, p2)])
        self.assertEqual(result, [
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'one'},
            {'role': 'tool', 'tool_call_id': 'call_2', 'content': 'two'},
        ])

    def test_failed_tool_result_prefixed_error(self):
        result_part = tool_result_part('search', 'call_1', [text_part('boom')], success=False)
        result = to_openai_messages(None, [user_message(result_part)])
        self.assertEqual(result[0]['content'], 'Error: boom')

    def test_tool_result_images_become_trailing_user_message(self):
        image = inline_data_part('image/png', 'QUJD')
        result_part = tool_result_part('screenshot', 'call_1', [text_part('here'), image])
        result = to_openai_messages(None, [user_message(result_part)])
        self.assertEqual(result, [
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'here'},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': 'Images returned by tool screenshot:'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,QUJD'}},
            ]},
        ])

    def test_only_tool_results_no_extra_user_message(self):
        result_part = tool_result_part('search', 'call_1', [text_part('found it')])
        result = to_openai_messages(None, [user_message(result_part)])
        roles = [m['role'] for m in result]
        self.assertEqual(roles, ['tool'])

    def test_assistant_text_only(self):
        message = assistant_message([text_part('hello there')])
        result = to_openai_messages(None, [message])
        self.assertEqual(result, [{'role': 'assistant', 'content': 'hello there'}])

    def test_assistant_text_and_tool_calls(self):
        message = assistant_message([
            text_part('let me check'),
            tool_call_part('search', {'q': 'é'}, 'call_1'),
        ])
        result = to_openai_messages(None, [message])
        self.assertEqual(result, [{
            'role': 'assistant',
            'content': 'let me check',
            'tool_calls': [{
                'id': 'call_1',
                'type': 'function',
                'function': {'name': 'search', 'arguments': json.dumps({'q': 'é'}, ensure_ascii=False)},
            }],
        }])
        arguments = result[0]['tool_calls'][0]['function']['arguments']
        self.assertIn('é', arguments)

    def test_assistant_only_tool_calls_content_is_none(self):
        message = assistant_message([tool_call_part('search', {'q': 'x'}, 'call_1')])
        result = to_openai_messages(None, [message])
        self.assertIsNone(result[0]['content'])

    def test_assistant_empty_text_no_tool_calls_sends_empty_string(self):
        message = assistant_message([])
        result = to_openai_messages(None, [message])
        self.assertEqual(result[0]['content'], '')

    def test_assistant_reasoning_details_echoed(self):
        message = assistant_message(
            [text_part('hi')],
            provider_metadata={'reasoning_details': [{'type': 'reasoning.text', 'text': 'thinking'}]},
        )
        result = to_openai_messages(None, [message])
        self.assertEqual(result[0]['reasoning_details'], [{'type': 'reasoning.text', 'text': 'thinking'}])

    def test_assistant_reasoning_string_echoed(self):
        message = assistant_message([text_part('hi')], provider_metadata={'reasoning': 'because'})
        result = to_openai_messages(None, [message])
        self.assertEqual(result[0]['reasoning'], 'because')

    def test_assistant_null_reasoning_not_echoed(self):
        """OpenRouter stores `reasoning: null` on most answers; another endpoint may refuse it."""
        message = assistant_message(
            [text_part('hi')], provider_metadata={'reasoning': None, 'reasoning_details': None})
        result = to_openai_messages(None, [message])
        self.assertNotIn('reasoning', result[0])
        self.assertNotIn('reasoning_details', result[0])

    def test_assistant_no_provider_metadata_no_reasoning_keys(self):
        message = assistant_message([text_part('hi')])
        result = to_openai_messages(None, [message])
        self.assertNotIn('reasoning', result[0])
        self.assertNotIn('reasoning_details', result[0])


class TestToOpenaiTools(BaseCase):

    def test_basic_tool_mapping(self):
        tools = [{'name': 'search', 'instructions': 'Search the web.', 'schema': {
            'type': 'object', 'properties': {'q': {'type': 'string'}}, 'required': ['q'],
        }}]
        result = to_openai_tools(tools)
        self.assertEqual(result, [{
            'type': 'function',
            'function': {
                'name': 'search',
                'description': 'Search the web.',
                'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}, 'required': ['q']},
            },
        }])

    def test_missing_type_defaults_to_object(self):
        tools = [{'name': 'noop', 'instructions': 'Does nothing.', 'schema': {}}]
        result = to_openai_tools(tools)
        self.assertEqual(result[0]['function']['parameters']['type'], 'object')

    def test_missing_properties_defaults_to_empty_dict(self):
        tools = [{'name': 'noop', 'instructions': 'Does nothing.', 'schema': {'type': 'object'}}]
        result = to_openai_tools(tools)
        self.assertEqual(result[0]['function']['parameters']['properties'], {})

    def test_does_not_mutate_input_schema(self):
        schema = {}
        tools = [{'name': 'noop', 'instructions': 'x', 'schema': schema}]
        to_openai_tools(tools)
        self.assertEqual(schema, {})


class TestToOpenaiResponseFormat(BaseCase):

    def test_none_schema_returns_none(self):
        self.assertIsNone(to_openai_response_format(None))

    def test_schema_wrapped_in_strict_json_schema(self):
        schema = {'type': 'object', 'properties': {}, 'additionalProperties': False, 'required': []}
        result = to_openai_response_format(schema)
        self.assertEqual(result, {
            'type': 'json_schema',
            'json_schema': {'name': 'response', 'strict': True, 'schema': schema},
        })

    def test_custom_name(self):
        result = to_openai_response_format({'type': 'object'}, name='my_schema')
        self.assertEqual(result['json_schema']['name'], 'my_schema')


class TestFromOpenaiResponse(BaseCase):

    def test_string_content_becomes_text_part(self):
        body = FakeTransport.text('hello world')[2]
        result = from_openai_response(body)
        self.assertEqual(result['message']['content'], [{'type': 'text', 'text': 'hello world'}])

    def test_list_content_becomes_text_parts(self):
        body = FakeTransport.text('ignored')[2]
        body['choices'][0]['message']['content'] = [
            {'type': 'text', 'text': 'part one'},
            {'type': 'text', 'text': 'part two'},
        ]
        result = from_openai_response(body)
        self.assertEqual(result['message']['content'], [
            {'type': 'text', 'text': 'part one'},
            {'type': 'text', 'text': 'part two'},
        ])

    def test_tool_calls_with_valid_json(self):
        body = FakeTransport.tool_calls([('search', {'q': 'cats'}, 'call_1')])[2]
        result = from_openai_response(body)
        self.assertEqual(result['message']['content'], [
            {'type': 'tool_call', 'name': 'search', 'args': {'q': 'cats'}, 'call_id': 'call_1'},
        ])

    def test_tool_calls_with_invalid_json(self):
        body = FakeTransport.tool_calls([('search', {'q': 'cats'}, 'call_1')])[2]
        body['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '{not json'
        result = from_openai_response(body)
        part = result['message']['content'][0]
        self.assertEqual(part['args'], {'__parse_error': '{not json'})

    def test_tool_calls_with_non_dict_json(self):
        body = FakeTransport.tool_calls([('search', {'q': 'cats'}, 'call_1')])[2]
        body['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '[1, 2, 3]'
        result = from_openai_response(body)
        part = result['message']['content'][0]
        self.assertEqual(part['args'], {'__parse_error': '[1, 2, 3]'})

    def test_tool_calls_truncates_long_raw_on_parse_error(self):
        body = FakeTransport.tool_calls([('search', {'q': 'cats'}, 'call_1')])[2]
        raw = '{' + 'x' * 600
        body['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = raw
        result = from_openai_response(body)
        part = result['message']['content'][0]
        self.assertEqual(len(part['args']['__parse_error']), 500)

    def test_images_become_inline_data_parts(self):
        body = FakeTransport.text('')[2]
        body['choices'][0]['message']['images'] = [
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,QUJD'}},
        ]
        result = from_openai_response(body)
        image_parts = [p for p in result['message']['content'] if p['type'] == 'inline_data']
        self.assertEqual(image_parts, [{'type': 'inline_data', 'mimetype': 'image/png', 'data': 'QUJD'}])

    def test_non_data_url_images_skipped(self):
        body = FakeTransport.text('')[2]
        body['choices'][0]['message']['images'] = [
            {'type': 'image_url', 'image_url': {'url': 'https://example.com/x.png'}},
        ]
        result = from_openai_response(body)
        image_parts = [p for p in result['message']['content'] if p['type'] == 'inline_data']
        self.assertEqual(image_parts, [])

    def test_reasoning_in_provider_metadata(self):
        body = FakeTransport.text('hi')[2]
        body['choices'][0]['message']['reasoning'] = 'because'
        body['choices'][0]['message']['reasoning_details'] = [{'type': 'reasoning.text', 'text': 'because'}]
        result = from_openai_response(body)
        self.assertEqual(result['message']['provider_metadata']['reasoning'], 'because')
        self.assertEqual(
            result['message']['provider_metadata']['reasoning_details'],
            [{'type': 'reasoning.text', 'text': 'because'}],
        )

    def test_annotations_in_provider_metadata(self):
        body = FakeTransport.text('hi')[2]
        body['choices'][0]['message']['annotations'] = [{'type': 'url_citation'}]
        result = from_openai_response(body)
        self.assertEqual(result['message']['provider_metadata']['annotations'], [{'type': 'url_citation'}])

    def test_provider_metadata_finish_reason_model_id(self):
        body = FakeTransport.text('hi', model='fake/model', request_id='gen-123', finish_reason='stop')[2]
        result = from_openai_response(body)
        metadata = result['message']['provider_metadata']
        self.assertEqual(metadata['finish_reason'], 'stop')
        self.assertEqual(metadata['model'], 'fake/model')
        self.assertEqual(metadata['id'], 'gen-123')

    def test_top_level_fields(self):
        body = FakeTransport.text('hi', model='fake/model', request_id='gen-123', finish_reason='stop')[2]
        result = from_openai_response(body)
        self.assertEqual(result['request_id'], 'gen-123')
        self.assertEqual(result['model'], 'fake/model')
        self.assertEqual(result['finish_reason'], 'stop')

    def test_empty_choices_raises_provider_error(self):
        body = FakeTransport.text('hi')[2]
        body['choices'] = []
        with self.assertRaises(ProviderError):
            from_openai_response(body)


class TestExtractUsage(BaseCase):

    def test_basic_usage(self):
        body = {'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15, 'cost': 0.002}}
        usage = extract_usage(body)
        self.assertEqual(usage['prompt_tokens'], 10)
        self.assertEqual(usage['completion_tokens'], 5)
        self.assertEqual(usage['total_tokens'], 15)
        self.assertEqual(usage['cost'], 0.002)

    def test_missing_usage_defaults_to_zero(self):
        usage = extract_usage({})
        self.assertEqual(usage, {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'cost': 0.0})

    def test_missing_total_tokens_computed_from_prompt_and_completion(self):
        body = {'usage': {'prompt_tokens': 3, 'completion_tokens': 4}}
        usage = extract_usage(body)
        self.assertEqual(usage['total_tokens'], 7)

    def test_cached_tokens_extracted(self):
        body = {'usage': {
            'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15, 'cost': 0.0,
            'prompt_tokens_details': {'cached_tokens': 4},
        }}
        usage = extract_usage(body)
        self.assertEqual(usage['cached_tokens'], 4)

    def test_reasoning_tokens_extracted(self):
        body = {'usage': {
            'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15, 'cost': 0.0,
            'completion_tokens_details': {'reasoning_tokens': 2},
        }}
        usage = extract_usage(body)
        self.assertEqual(usage['reasoning_tokens'], 2)

    def test_no_cached_or_reasoning_tokens_keys_absent(self):
        body = {'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2, 'cost': 0.0}}
        usage = extract_usage(body)
        self.assertNotIn('cached_tokens', usage)
        self.assertNotIn('reasoning_tokens', usage)


class TestFromOpenaiEmbeddings(BaseCase):

    def test_vectors_ordered_by_index(self):
        body = {
            'model': 'fake/embed',
            'data': [
                {'index': 1, 'embedding': [0.2, 0.2]},
                {'index': 0, 'embedding': [0.1, 0.1]},
            ],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 0, 'total_tokens': 2, 'cost': 0.0},
        }
        result = from_openai_embeddings(body)
        self.assertEqual(result['vectors'], [[0.1, 0.1], [0.2, 0.2]])
        self.assertEqual(result['model'], 'fake/embed')
        self.assertEqual(result['usage']['completion_tokens'], 0)


class TestRoundTrip(BaseCase):

    def test_tool_call_round_trips_through_openai_format(self):
        message = assistant_message([
            text_part('checking'),
            tool_call_part('search', {'q': 'cats', 'limit': 3}, 'call_abc'),
        ])
        openai_messages = to_openai_messages(None, [message])

        # Simulate the provider echoing back the same tool call in its response.
        openai_call = openai_messages[0]['tool_calls'][0]
        body = FakeTransport.tool_calls([
            (openai_call['function']['name'], json.loads(openai_call['function']['arguments']), openai_call['id']),
        ])[2]

        result = from_openai_response(body)
        call_part = result['message']['content'][0]
        self.assertEqual(call_part['name'], 'search')
        self.assertEqual(call_part['args'], {'q': 'cats', 'limit': 3})
        self.assertEqual(call_part['call_id'], 'call_abc')
