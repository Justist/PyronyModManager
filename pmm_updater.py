from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal

# Point this at your own repo once you publish releases.
_RELEASES_API = "https://api.github.com/repos/Justist/PyronyModManager/releases/latest"
CURRENT_VERSION = "0.1.0"


@dataclass
class ReleaseInfo:
   version: str
   html_url: str
   notes: str  # raw markdown from GitHub


class UpdateChecker(QThread):
   """
   Background thread that queries the GitHub releases API once.
   Exactly one of the three signals fires after run() completes.
   """

   update_available = Signal(object)  # ReleaseInfo
   up_to_date = Signal()
   check_failed = Signal(str)  # human-readable error

   def __init__(self, releases_api: str = _RELEASES_API, parent=None) -> None:
      super().__init__(parent)
      self._api = releases_api

   def run(self) -> None:
      try:
         import httpx
         resp = httpx.get(self._api, timeout=8, follow_redirects=True)
         resp.raise_for_status()
         data = resp.json()
         tag = data.get("tag_name", "").lstrip("v")
         if _is_newer(tag, CURRENT_VERSION):
            self.update_available.emit(
               ReleaseInfo(
                  version=tag,
                  html_url=data.get("html_url", ""),
                  notes=data.get("body", ""),
               )
            )
         else:
            self.up_to_date.emit()
      except Exception as exc:  # noqa: BLE001
         self.check_failed.emit(str(exc))


def _is_newer(remote: str, current: str) -> bool:
   """Return True when remote version tuple is strictly greater than current."""

   def parse(v: str) -> tuple[int, ...]:
      parts = []
      for x in v.split("."):
         if x.isdigit():
            parts.append(int(x))
      return tuple(parts)

   return parse(remote) > parse(current)
