DOMAIN = "ondol"

SUBENTRY_ROOM = "room"

# hub
CONF_PRESENCE = "presence_entity"  # on = nobody home (certain)
CONF_DAYCARE = "daycare_entity"  # on = the child is out (daycare)
CONF_OUTDOOR = "outdoor_entity"
CONF_TOLERANCE = "tolerance"
CONF_WON_PER_HOUR = "won_per_hour"

# room
CONF_NAME = "name"
CONF_CLIMATE = "climate_entity"
CONF_SCHEDULE = "schedule"
CONF_FOLLOWS_DAYCARE = "follows_daycare"

PRESETS = ("comfort", "sleep", "eco", "away")
PRESET_LABELS = {"comfort": "편안", "sleep": "수면", "eco": "절약", "away": "외출"}

DEFAULT_TOLERANCE = 0.5
DEFAULT_PRESETS = {"comfort": 23.0, "sleep": 23.0, "eco": 21.0, "away": 20.0}
DEFAULT_SCHEDULE = "월-금 07:00 편안, 09:00 절약, 18:00 편안, 23:00 수면\n토-일 08:00 편안, 23:00 수면"

# never write a setpoint below this, so a room left alone (by us or by a failure) stays warm
MIN_SETPOINT = 18.0
MAX_SETPOINT = 30.0

EVENT_ONDOL = f"{DOMAIN}_action"
