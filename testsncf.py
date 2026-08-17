import requests
from google.transit import gtfs_realtime_pb2
from datetime import datetime

URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"

TRAIN_NUMBER = "16840"

print("Downloading SNCF real-time feed...")

response = requests.get(URL, timeout=30)
response.raise_for_status()

feed = gtfs_realtime_pb2.FeedMessage()
feed.ParseFromString(response.content)

print(f"Feed timestamp: {datetime.fromtimestamp(feed.header.timestamp)}")
print(f"Entities: {len(feed.entity)}")
print()

found = False

for entity in feed.entity:

    if not entity.HasField("trip_update"):
        continue

    trip = entity.trip_update.trip

    # Print the trip information so we can understand SNCF's identifiers
    trip_id = trip.trip_id
    start_date = trip.start_date
    start_time = trip.start_time

    # Search all string fields for the commercial train number
    trip_text = str(trip)

    if TRAIN_NUMBER not in trip_text:
        continue

    found = True

    print("=" * 70)
    print("FOUND TRAIN", TRAIN_NUMBER)
    print("=" * 70)

    print(f"trip_id:     {trip_id}")
    print(f"start_date:  {start_date}")
    print(f"start_time:  {start_time}")
    print()

    for stop in entity.trip_update.stop_time_update:

        print(f"stop_sequence: {stop.stop_sequence}")
        print(f"stop_id:       {stop.stop_id}")

        if stop.HasField("arrival"):
            print(f"arrival delay: {stop.arrival.delay} sec")

            if stop.arrival.time:
                print(
                    "arrival time:  ",
                    datetime.fromtimestamp(stop.arrival.time)
                )

        if stop.HasField("departure"):
            print(f"departure delay: {stop.departure.delay} sec")

            if stop.departure.time:
                print(
                    "departure time:",
                    datetime.fromtimestamp(stop.departure.time)
                )

        print("-" * 40)

if not found:
    print(f"Train {TRAIN_NUMBER} was NOT found in this feed.")