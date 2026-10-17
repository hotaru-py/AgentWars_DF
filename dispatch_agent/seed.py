"""Seed data for the Chennai dispatch demo. Shared and read-only: every track imports it.

Coordinates are approximate. Customer names and phone numbers are fake.
"""

SPEED_KMPH = 25      # average city van speed
ROAD_FACTOR = 1.3    # haversine km -> road km

DEPOT = {"id": "DEPOT", "area": "Guindy", "lat": 13.0067, "lng": 80.2206}

ORDERS = [
    {"id": "O1",  "customer": "Arun Kumar",    "phone": "+91-90000-00001", "area": "T. Nagar",             "lat": 13.0418, "lng": 80.2341, "deadline_min": 90,  "priority": "normal", "weight_kg": 4},
    {"id": "O2",  "customer": "Divya Raman",   "phone": "+91-90000-00002", "area": "Anna Nagar",           "lat": 13.0850, "lng": 80.2101, "deadline_min": 120, "priority": "urgent", "weight_kg": 6},
    {"id": "O3",  "customer": "Karthik S",     "phone": "+91-90000-00003", "area": "Adyar",                "lat": 13.0012, "lng": 80.2565, "deadline_min": 100, "priority": "normal", "weight_kg": 3},
    {"id": "O4",  "customer": "Meena Iyer",    "phone": "+91-90000-00004", "area": "Velachery",            "lat": 12.9815, "lng": 80.2180, "deadline_min": 80,  "priority": "normal", "weight_kg": 8},
    {"id": "O5",  "customer": "Rahul Nair",    "phone": "+91-90000-00005", "area": "Tambaram",             "lat": 12.9249, "lng": 80.1000, "deadline_min": 150, "priority": "normal", "weight_kg": 12},
    {"id": "O6",  "customer": "Priya Venkat",  "phone": "+91-90000-00006", "area": "Sholinganallur (OMR)", "lat": 12.9010, "lng": 80.2279, "deadline_min": 140, "priority": "urgent", "weight_kg": 5},
    {"id": "O7",  "customer": "Suresh Babu",   "phone": "+91-90000-00007", "area": "Porur",                "lat": 13.0382, "lng": 80.1565, "deadline_min": 110, "priority": "normal", "weight_kg": 7},
    {"id": "O8",  "customer": "Lakshmi P",     "phone": "+91-90000-00008", "area": "Mylapore",             "lat": 13.0368, "lng": 80.2676, "deadline_min": 130, "priority": "normal", "weight_kg": 2},
    {"id": "O9",  "customer": "Vignesh R",     "phone": "+91-90000-00009", "area": "Egmore",               "lat": 13.0732, "lng": 80.2609, "deadline_min": 120, "priority": "normal", "weight_kg": 5},
    {"id": "O10", "customer": "Anitha Joseph", "phone": "+91-90000-00010", "area": "Thiruvanmiyur (OMR)",  "lat": 12.9830, "lng": 80.2594, "deadline_min": 100, "priority": "normal", "weight_kg": 4},
]

VEHICLES = [
    {"id": "V1", "driver": "Ganesh", "capacity_kg": 50, "status": "active"},
    {"id": "V2", "driver": "Farhan", "capacity_kg": 50, "status": "active"},
    {"id": "V3", "driver": "Selvi",  "capacity_kg": 50, "status": "active"},
]

# Disruption scenarios. The multiplier is added at run time: the predicted value
# comes from the policy, the true value from feedback.TRUTH.
SCENARIOS = {
    "flood": {"kind": "flood", "label": "Flooding on OMR",
              "center": [12.942, 80.243], "radius_km": 5.0, "vehicle_id": None},
    "traffic": {"kind": "traffic", "label": "Traffic jam in T. Nagar",
                "center": [13.039, 80.251], "radius_km": 2.5, "vehicle_id": None},
    "breakdown": {"kind": "breakdown", "label": "Van V2 broke down",
                  "center": None, "radius_km": 0.0, "vehicle_id": "V2"},
}
