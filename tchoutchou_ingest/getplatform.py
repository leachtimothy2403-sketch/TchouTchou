import requests
import xml.etree.ElementTree as ET

URL = "https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-estimated-timetable"

response = requests.get(URL, timeout=30)
response.raise_for_status()

root = ET.fromstring(response.content)

NS = {"siri": "http://www.siri.org.uk/siri"}

# Find every estimated journey
journeys = root.findall(
    ".//siri:EstimatedVehicleJourney",
    NS
)

print("Journeys:", len(journeys))

for journey in journeys:

    train_numbers = [
        x.text
        for x in journey.findall(
            ".//siri:TrainNumberRef",
            NS
        )
    ]

    origin = journey.findtext("siri:OriginName", namespaces=NS)
    destination = journey.findtext("siri:DestinationName", namespaces=NS)

    print("\n----------------------------")
    print("Train:", train_numbers)
    print("Origin:", origin)
    print("Destination:", destination)

    # Print every recorded call
    for call in journey.findall(".//siri:RecordedCall", NS):

        stop_name = call.findtext(
            "siri:StopPointName",
            namespaces=NS
        )

        stop_ref = call.findtext(
            "siri:StopPointRef",
            namespaces=NS
        )

        print("  Stop:", stop_name)
        print("  Stop ref:", stop_ref)

        # Print all child elements so we can identify the platform field
        for child in call.iter():
            if child is not call:
                print("    ", child.tag.split("}")[-1], "=", child.text)