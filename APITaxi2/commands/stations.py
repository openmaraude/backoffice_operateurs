import collections
import csv
import datetime
import json
import logging
import re

import click
from flask import Blueprint
from geopy.geocoders import Nominatim
import pyproj
from shapely.geometry import Point
from shapely.ops import transform

from APITaxi_models2 import db, Station, Town


blueprint = Blueprint('commands_stations', __name__, cli_group=None)

logger = logging.getLogger(__name__)

now = datetime.datetime.now(datetime.timezone.utc).isoformat()

wgs84 = pyproj.CRS('EPSG:4326')
lambert93 = pyproj.CRS('EPSG:2154')
projection = pyproj.Transformer.from_crs(lambert93, wgs84, always_xy=True).transform

geocoder = Nominatim(user_agent="le.taxi")

STATIONSTAXI_PATTERN = re.compile(r'^stationstaxi_(\w+)_(\d{4})(\d{2})(\d{2})\.csv$')


def decode_geopoint(geopoint):
    try:
        geopoint = json.loads(geopoint)
    except json.JSONDecodeError:
        # Let it fail
        xlong, ylat = re.match(r'^(\d+.?\d+?),\ ?(\d+.?\d+?)$', geopoint).groups()
    else:
        if isinstance(geopoint, dict):
            xlong, ylat = geopoint['lon'], geopoint['lat']
        elif isinstance(geopoint, list):
            xlong, ylat = geopoint
        else:
            raise ValueError("Invalid geopoint format")

    return xlong, ylat


def _update_or_create(station_id, name, address, insee, places, lon, lat, call_number, info, mtime=None, source=None):
    if not lon or not lat:
        logger.warning('Ignoring station ID %s with missing coordinates', station_id)
        return None
    # Normalize station ID
    station_insee, station_increment = station_id.split('-T-')
    assert station_insee == insee
    station_id = f"{station_insee}-T-{int(station_increment):03}"
    created = False
    station = db.session.query(Station).filter(Station.id == station_id).one_or_none()
    # Grenoble already using the station ID convention
    if not station:
        # Create
        station = Station(
            id=station_id,
            added_at=now)
        created = True
    # Update
    station.name = name.strip()
    station.address = address.strip()
    station.town = db.session.query(Town).filter(Town.insee == insee).one()  # Let it fail
    station.places = int(places or 0)
    station.location = f'POINT({lon} {lat})'
    # Normalize (use a python lib?)
    station.call_number = "".join(call_number.split()) if call_number else None
    station.info = info
    # HistoryMixin
    station.added_via = 'api'
    station.source = source or 'AOM'
    station.last_update_at = mtime or now
    db.session.add(station)
    return created


def import_antibes_stations(csv_reader):
    counter = collections.Counter()
    for commune, siret, numero, adresse, complement, lat, lon, mtime in csv_reader:
        station_id = f'06004-T-{numero}'
        if not numero:
            logger.warning('Ignoring station at %s with no identifier', adresse)
            counter[None] += 1
            continue
        mtime = datetime.date(*map(int, mtime.split('-')))
        counter[_update_or_create(station_id, complement or adresse, adresse, '06004', 0, lon, lat, None, "", mtime)] += 1
    return counter


def import_bayonne_stations(geojson):
    counter = collections.Counter()
    for feature in geojson['features']:
        props = feature['properties']
        if props['nature_gra'] != "taxis":
            continue
        lon, lat = feature['geometry']['coordinates']
        insee = str(props['code_insee'])
        station_id = f"{insee}-T-{props['ogc_fid']}"
        counter[_update_or_create(station_id, props['adresse'], props['adresse'], insee, props['nbre_de_pl'], lon, lat, None, "")] += 1
    return counter


def import_brest_stations(csv_reader):
    # This one is quite complicated...
    # We have the individual places, pick the first one, the others increment the number of places
    stations = {}
    for lon93, lat93, _, noarr, typearr, _, _, _, _, _, _, _, _, _, _, _, _, _, _, mtime in csv_reader:
        if typearr != 'STA_TAX':
            continue
        if noarr in stations:
            stations[noarr][2] += 1
        else:
            lambert93_point = Point(float(lon93), float(lat93))
            wgs84_point = transform(projection, lambert93_point)
            lon, lat = wgs84_point.x, wgs84_point.y
            try:
                name, address = {
                    '8573': ("Prat Lédan", "Prat Lédan"),
                    '8608': ("Brest Arena", "Rue de Quélern"),
                    '9013': ("Gares", "Place du 19è RI"),
                    '9123': ("Patinoire", "Avenue de Tarente"),  # XXX
                    '9148': ("rue Kerfautras", "rue Kerfautras"),
                    '9302': ("Multiplexe Liberté", "Avancée Porte St Louis"),
                    '9303': ("Bas de Siam", "Bas de Siam"),
                    '9421': ("Rue Albert Rolland", "Rue Albert Rolland"),
                    '9543': ("Rue docteur Roux", "Rue docteur Roux"),
                    '9550': ("Recouvrance", "Rue Bouillon"),
                    '9797': ("Patinoire", "Avenue de Tarente"),
                    '9906': ("Place de Strasbourg", "Place de Strasbourg"),
                }[noarr]
            except KeyError:
                # Newer stations
                location = geocoder.reverse((lat, lon), exactly_one=True, language='fr-fr,fr')
                name = location.raw['address']['amenity']
                address = location.raw['address']['road']

            mtime = datetime.datetime.strptime(mtime, '%Y/%m/%d %H:%M:%S').date()
            stations[noarr] = [name, address, 1, lon, lat, mtime]

    counter = collections.Counter()
    # XXX noarr is not usable as we need an three-digit increment
    for i, (station_id, (name, address, places, lon, lat, mtime)) in enumerate(stations.items()):
        station_id = f'29019-T-{i}'
        counter[_update_or_create(station_id, name, address, '29019', places, lon, lat, None, "", mtime)] += 1
    return counter


def import_grenoble_stations(csv_reader):
    counter = collections.Counter()
    for station_id, name, insee, address, places, call_number, lon, lat, info in csv_reader:
        # The Grenoble format is pretty much what we want
        counter[_update_or_create(station_id, name, address, insee, places, lon, lat, call_number, info)] += 1
    return counter


def import_lorient_stations(csv_reader):
    counter = collections.Counter()
    # XXX no unique identifier
    i = 1
    for (_, _, insee, commune, _, _, _, _, _, voie_nom, _, adresse, stationnement_type, stationnement_position, _, _, _, lon, lat, _, date_maj, _, _) in csv_reader:
        if stationnement_type != "Taxi":
            continue
        station_id = f"{insee}-T-{i:03}"
        mtime = datetime.date(*map(int, date_maj.split('-')))
        counter[_update_or_create(station_id, voie_nom, adresse, insee, 0, lon, lat, None, stationnement_position, mtime)] += 1
        i += 1
    return counter


def import_lyon_stations(geojson):
    counter = collections.Counter()
    for feature in geojson['features']:
        lon, lat = feature['geometry']['coordinates']
        props = feature['properties']
        station_id = f"69123-T-{props['gid']}"
        counter[_update_or_create(station_id, props['nom'], props['adresse'], '69123', props['nbemplacements'], lon, lat, None, "")] += 1
    return counter


def import_marseille_stations(csv_reader):
    counter = collections.Counter()
    # XXX no unique identifier
    for i, (nom, type_site, adresse1, adresse2, code_postal, ville, contact, no_telephone, lon, lat, mtime) in enumerate(csv_reader):
        mtime = datetime.datetime.strptime(mtime, '%d/%m/%Y %H:%M:%S').date()
        station_id = f'13055-T-{i:03}'
        counter[_update_or_create(station_id, nom, adresse1, '13055', 0, lon, lat, no_telephone or None, adresse2, mtime)] += 1
    return counter


def import_paris_stations(csv_reader):
    counter = collections.Counter()
    for station_id, name, address, _postcode, call_number, _xlb93, _ylb93, _feature, point in csv_reader:
        # Normalize station ID to our convention
        station_id = f'75056-T-{station_id}'
        lat, lon = point.split(',')
        counter[_update_or_create(station_id, name, address, '75056', 0, lon, lat, call_number, "")] += 1
    return counter


def import_rouen_stations(csv_reader):
    counter = collections.Counter()
    for station_id, name, insee, address, places, call_number, lon, lat, mtime in csv_reader:
        mtime = datetime.date(*map(int, reversed(mtime.split('/'))))
        counter[_update_or_create(station_id, name, address, insee, places, lon, lat, call_number, "", mtime)] += 1
    return counter


def import_toulouse_stations(csv_reader):
    counter = collections.Counter()
    # XXX no unique identifier
    for i, (geo_point_2d, _, no, suffixe, lib_voie, motdir, commune, insee, nb_places, _, _, _) in enumerate(csv_reader):
        station_id = f"{insee}-T-{i:03}"
        # coord_x and coord_y contain errors, use another column
        lat, lon = geo_point_2d.split(',')
        address = f"{no} {lib_voie} {commune}"
        counter[_update_or_create(station_id, motdir, address, insee, nb_places, lon, lat, None, "")] += 1
    return counter


def import_stationstaxi_stations(csv_reader, mtime=None, source=None):
    counter = collections.Counter()
    for station_id, name, insee, geopoint, address, places, call_number, info in csv_reader:
        xlong, ylat = decode_geopoint(geopoint)
        counter[_update_or_create(station_id, name, address, insee, places, xlong, ylat, call_number, info, mtime=mtime, source=source)] += 1
    return counter


PROVIDERS = {
    'antibes': {
        'encoding': "Windows-1252",
        'importer': import_antibes_stations,
    },
    'bayonne': {
        'encoding': "utf-8",
        'importer': import_bayonne_stations,
        'geojson': True,
    },
    'brest': {
        'encoding': "utf-8",
        'importer': import_brest_stations,
    },
    'grenoble': {
        'encoding': "Windows-1252",
        'importer': import_grenoble_stations,
    },
    'le.taxi': {  # for when I forged the file in the Grenoble format but UTF-8
        'encoding': "utf-8",
        'importer': import_grenoble_stations,
    },
    'lorient': {
        'encoding': "utf-8",
        'importer': import_lorient_stations,
    },
    'lyon': {
        'encoding': "utf-8",
        'importer': import_lyon_stations,
        'geojson': True,
    },
    'marseille': {
        'encoding': "utf-8",
        'importer': import_marseille_stations,
    },
    'paris': {
        'encoding': "utf-8",
        'importer': import_paris_stations,
    },
    'rouen': {  # I forged the file in the Grenoble format (plus mtime)
        'encoding': "utf-8",
        'importer': import_rouen_stations,
    },
    'toulouse': {
        'encoding': "utf-8",
        'importer': import_toulouse_stations,
    },
    'schema_data_gouv': {  # Our standard schema
        'encoding': "utf-8",
        'importer': import_stationstaxi_stations,
        'mtime_source': True,
    },
}


def valid_provider(value):
    if value not in PROVIDERS:
        raise click.BadParameter('Provider %s is not supported' % value)
    return PROVIDERS[value]


@blueprint.cli.command('import_stations')
@click.argument('provider', required=True, type=valid_provider)
@click.argument('filename', required=True, type=click.Path(exists=True, dir_okay=False))
def import_stations(provider, filename):
    # Our own schema contains metadata in the filename
    source = None
    mtime = None
    match = STATIONSTAXI_PATTERN.match(filename)
    if match:
        source, year, month, day = match.groups()
        mtime = datetime.date(int(year), int(month), int(day))  # Let it fail

    with open(filename, encoding=provider['encoding']) as handler:
        if provider.get('geojson'):
            reader = json.load(handler)
        else:
            sniffer = csv.Sniffer()
            sample = handler.read()
            dialect = sniffer.sniff(sample)
            handler.seek(0)
            reader = csv.reader(handler, dialect=dialect)
            if sniffer.has_header(sample):
                next(reader)

        if provider.get('mtime_source'):
            counter = provider['importer'](reader, mtime=mtime, source=source)
        else:
            counter = provider['importer'](reader)

        print(f'{filename}: {counter[True]} stations created, {counter[False]} updated, {counter[None]} ignored.')

    db.session.commit()
