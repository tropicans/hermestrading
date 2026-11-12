import os
from datetime import datetime, timezone

class NewsBlackoutFilter:
    def __init__(self):
        self.before_mins = int(os.getenv("NEWS_BLACKOUT_MINUTES_BEFORE", 30))
        self.after_mins = int(os.getenv("NEWS_BLACKOUT_MINUTES_AFTER", 30))
        # Blackout windows statis/mock (akan disinkronkan via data kalender API)
        self.scheduled_events = []

    def set_blackout_events(self, events: list):
        self.scheduled_events = events

    def is_in_blackout(self) -> tuple[bool, str]:
        now = datetime.now(timezone.utc).timestamp()
        
        for ev in self.scheduled_events:
            event_time = ev.get("timestamp", 0)
            name = ev.get("title", "High-Impact Economic News")
            start_window = event_time - (self.before_mins * 60)
            end_window = event_time + (self.after_mins * 60)

            if start_window <= now <= end_window:
                return True, f"Jendela Blackout aktif untuk agenda: '{name}'"

        return False, "Kondisi pasar bebas dari agenda blackout berita."
