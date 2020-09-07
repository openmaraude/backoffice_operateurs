from APITaxi_models2 import ZUPC
from APITaxi_models2.unittest.factories import (
    ZUPCFactory,
)


class TestZUPCList:
    def test_invalid(self, anonymous, moteur, operateur):
        # Login required
        resp = anonymous.client.get('/zupc')
        assert resp.status_code == 401

        # Permissions OK, but the required querystring arguments ?lon and ?lat
        # are not provided
        resp = operateur.client.get('/zupc')
        assert resp.status_code == 400
        assert 'lon' in resp.json['errors']
        assert 'lat' in resp.json['errors']

    def test_ok(self, moteur):
        # lon=2.35&lat=48.86 = location in middle of Paris. No ZUPC is created
        # yet, so the result is empty.
        resp = moteur.client.get('zupc?lon=2.35&lat=48.86')
        assert resp.status_code == 200
        assert resp.json == {'data': []}

        # ZUPCFactory creates the Paris ZUPC.
        zupc = ZUPCFactory()

        resp = moteur.client.get('zupc?lon=2.35&lat=48.86')
        assert resp.status_code == 200
        assert resp.json['data'][0] == {
            'active': zupc.active,
            'insee': zupc.insee,
            'nom': zupc.nom
        }

        # XXX: when InfluxDB is available and the number of active taxis in the
        # INSEE code (filled by a cron job) is present, the response of GET
        # /zupc contains a field "nb_active".
        # For now, unittests do not run InfluxDB so this field is absent.
