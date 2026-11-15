import json
import unittest

import ozon_posting_status as posting


class PostingStatusTests(unittest.TestCase):
    def setUp(self):
        self.creds = {'client_id': '123', 'api_key': 'synthetic-secret'}
        self.args = {'date_from': '2026-11-02', 'date_to': '2026-11-08', 'scheme': 'FBO'}

    def test_fixed_endpoints_and_explicit_utc_period(self):
        for scheme, version in (('FBO', 'v3/posting/fbo/list'), ('FBS', 'v4/posting/fbs/list')):
            args = dict(self.args, scheme=scheme)
            request = posting.build_request(args, self.creds)
            self.assertEqual(request.full_url, 'https://api-seller.ozon.ru/' + version)
            data = json.loads(request.data)
            self.assertEqual(data['filter'], {'since': '2026-11-02T00:00:00Z',
                                               'to': '2026-11-08T23:59:59.999999999Z'})
            self.assertEqual(data['limit'], 100)
            self.assertFalse(any(data['with'].values()))

    def test_counts_use_status_and_units_without_exposing_provider_details(self):
        data = {'postings': [
            {'status': 'delivered', 'posting_number': 'DO-NOT-SHARE',
             'products': [{'quantity': 2, 'name': 'PRIVATE', 'price': 123}]},
            {'status': 'delivered', 'products': [{'quantity': 1}]},
            {'status': 'cancelled', 'products': [{'quantity': 3}]},
            {'status': 'new_unknown', 'products': []}], 'has_next': False}
        result = posting.project_response(self.args, data, self.creds)
        self.assertEqual(result['page_counts'], {'delivered': {'postings': 2, 'units': 3},
                         'cancelled': {'postings': 1, 'units': 3}, 'other': {'postings': 1, 'units': 0}})
        self.assertTrue(result['complete'])
        self.assertFalse(result['has_more'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('DO-NOT-SHARE', json.dumps(result))
        self.assertNotIn('new_unknown', json.dumps(result))

    def test_pagination_and_envelope(self):
        data = {'result': {'postings': [{'status': 'delivering', 'products': [{'quantity': 1}]}],
                           'has_next': True, 'cursor': 'page-two'}}
        result = posting.project_response(self.args, data, self.creds)
        self.assertEqual(result['next_cursor'], 'page-two')
        self.assertFalse(result['complete'])
        page_two = posting.project_response(dict(self.args, cursor='page-two'),
                    {'postings': [], 'has_next': False}, self.creds)
        self.assertIsNone(page_two['next_cursor'])
        self.assertFalse(page_two['complete'])

    def test_invalid_arguments_and_unsafe_cursors(self):
        bad = ({'url': 'https://evil'}, {'scheme': 'all'}, {'scheme': []},
               {'date_from': '2026-02-30'}, {'date_from': '2026-11-09'},
               {'date_from': '2026-01-01'}, {'limit': True}, {'limit': 101}, {'cursor': 'x\ny'})
        for values in bad:
            with self.subTest(values=values), self.assertRaises(ValueError):
                posting.validate_args(dict(self.args, **values))
        for cursor in ('', 'synthetic-secret', 'cursor\nheader'):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                posting.project_response(self.args, {'has_next': True, 'cursor': cursor,
                    'postings': [{'status': 'delivered', 'products': []}]}, self.creds)

    def test_missing_pagination_and_wrong_quantities_are_errors(self):
        invalid = ({'postings': [], 'has_next': 1},
                   {'postings': [], 'has_next': True, 'cursor': 'next'},
                   {'postings': [{'status': 'delivered', 'products': [{'quantity': True}]}], 'has_next': False},
                   {'postings': [{'status': 'delivered', 'products': [{'quantity': -1}]}], 'has_next': False},
                   {'postings': [{'status': 'delivered'}], 'has_next': False})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                posting.project_response(self.args, value, self.creds)


if __name__ == '__main__':
    unittest.main()
