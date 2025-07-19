from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import datetime
import os.path
import pandas as pd
from geopy.geocoders import Nominatim
import time
import requests
import folium
from tqdm import tqdm
import re

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
DB_PATH = 'bbdd/locations.csv'
BLACKLIST_PATH = 'bbdd/blacklist_locations.csv'
OPEN_CAGE_KEY_PATH = 'credentials/opencage.key'

AIRPORT_REGEX = re.compile(r'\b[A-Z]{3}\b')


def load_opencage_api_key():
  if os.path.exists(OPEN_CAGE_KEY_PATH):
    with open(OPEN_CAGE_KEY_PATH, 'r') as f:
      return f.read().strip()
  else:
    raise FileNotFoundError(
        "OpenCage API key file not found: 'credentials/opencage.key'")


def load_blacklist_locations():
  if os.path.exists(BLACKLIST_PATH):
    return pd.read_csv(BLACKLIST_PATH)['location'].dropna().tolist()
  else:
    return []


def extract_event_data(event):
  start = event['start'].get('dateTime', event['start'].get('date'))
  end = event['end'].get('dateTime', event['end'].get('date'))
  summary = event.get('summary', 'No title')
  location = event.get('location', None)
  calendar = event.get('calendarSummary', '')
  return {
      'start': start,
      'end': end,
      'summary': summary,
      'location': location,
      'calendar': calendar
  }


def geocode_locations_geopy(locations):
  geolocator = Nominatim(user_agent="calendar_geo")
  coords = {}
  for loc in tqdm(locations, desc="Geocoding with geopy"):
    try:
      location = geolocator.geocode(loc)
      if location:
        coords[loc] = (location.latitude, location.longitude)
      else:
        coords[loc] = (None, None)
      time.sleep(1)
    except Exception:
      coords[loc] = (None, None)
  return coords


def geocode_locations_opencage(locations, api_key):
  coords = {}
  for loc in tqdm(locations, desc="Geocoding with OpenCage"):
    try:
      response = requests.get('https://api.opencagedata.com/geocode/v1/json',
                              params={
                                  'q': loc,
                                  'key': api_key,
                                  'limit': 1,
                                  'no_annotations': 1
                              })
      data = response.json()
      if data['results']:
        result = data['results'][0]
        confidence = result.get('confidence', 0)
        if confidence >= 7:
          lat = result['geometry']['lat']
          lon = result['geometry']['lng']
          coords[loc] = (lat, lon)
        else:
          coords[loc] = (None, None)
      else:
        coords[loc] = (None, None)
      time.sleep(1)
    except Exception:
      coords[loc] = (None, None)
  return coords


def load_location_database():
  if os.path.exists(DB_PATH):
    return pd.read_csv(DB_PATH)
  else:
    return pd.DataFrame(columns=['location', 'lat', 'lon'])


def save_location_database(df):
  os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
  df.to_csv(DB_PATH, index=False)


def plot_map(df_locations):
  df_clean = df_locations.dropna(subset=['lat', 'lon']).drop_duplicates(
      subset=['lat', 'lon'])
  if df_clean.empty:
    print("No valid coordinates found for plotting.")
    return
  first_point = df_clean.iloc[0]
  m = folium.Map(location=[first_point['lat'], first_point['lon']],
                 zoom_start=4)
  for _, row in df_clean.iterrows():
    folium.Marker(location=[row['lat'], row['lon']],
                  popup=row['location'],
                  icon=folium.Icon(color='blue', icon='info-sign')).add_to(m)
  m.save('location_map.html')
  print("Map saved as: location_map.html")


def get_authenticated_service():
  creds = None
  token_path = 'credentials/google_calendar_token.json'
  if os.path.exists(token_path):
    creds = Credentials.from_authorized_user_file(token_path, SCOPES)

  if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
      creds.refresh(Request())
    else:
      flow = InstalledAppFlow.from_client_secrets_file(
          'credentials/google_calendar_credentials.json', SCOPES)
      creds = flow.run_console()
    with open(token_path, 'w') as token:
      token.write(creds.to_json())

  return build('calendar', 'v3', credentials=creds)


def fetch_calendar_events(service):
  now = datetime.datetime.utcnow().isoformat() + 'Z'
  min_time = '2010-01-01T00:00:00Z'

  calendar_list = service.calendarList().list().execute().get('items', [])
  selected_calendars = [
      'ainmfiqd4giaurnk30kuoupduo@group.calendar.google.com',
      'cpol5h37rdg175upqfbbj2t1o0@group.calendar.google.com',
      'cromerovargas2d@gmail.com',
  ]
  calendar_list = [
      cal for cal in calendar_list if cal['id'] in selected_calendars
  ]

  all_events = []
  for calendar in calendar_list:
    calendar_id = calendar['id']
    page_token = None
    while True:
      events_result = service.events().list(calendarId=calendar_id,
                                            timeMin=min_time,
                                            timeMax=now,
                                            singleEvents=True,
                                            maxResults=250,
                                            pageToken=page_token).execute()
      events = events_result.get('items', [])
      for event in events:
        event['calendarSummary'] = calendar.get('summary', calendar_id)
        all_events.append(event)
      page_token = events_result.get('nextPageToken')
      if not page_token:
        break

  all_events.sort(
      key=lambda e: e['start'].get('dateTime', e['start'].get('date')),
      reverse=True)

  return [extract_event_data(e) for e in all_events]


def process_and_plot_events():
  service = get_authenticated_service()
  events_data = fetch_calendar_events(service)
  df = pd.DataFrame(events_data)

  db_locations = load_location_database()
  blacklist_locations = load_blacklist_locations()
  unique_locations = df['location'].dropna().unique()
  known_locations = db_locations['location'].tolist()

  def is_valid_location(loc):
    return (',' in loc
            or AIRPORT_REGEX.search(loc)) and loc not in blacklist_locations

  new_locations = [
      loc for loc in unique_locations
      if loc not in known_locations and is_valid_location(loc)
  ]

  opencage_api_key = load_opencage_api_key()
  new_coords = geocode_locations_opencage(new_locations, opencage_api_key)

  new_entries = pd.DataFrame([{
      'location': loc,
      'lat': lat,
      'lon': lon
  } for loc, (lat, lon) in new_coords.items()])

  updated_db = pd.concat([db_locations, new_entries], ignore_index=True)
  save_location_database(updated_db)

  coord_map = updated_db.set_index('location')[['lat', 'lon']].to_dict('index')
  df['lat'] = df['location'].map(lambda loc: coord_map.get(loc, {}).get('lat'))
  df['lon'] = df['location'].map(lambda loc: coord_map.get(loc, {}).get('lon'))

  plot_map(updated_db)


if __name__ == '__main__':
  process_and_plot_events()
