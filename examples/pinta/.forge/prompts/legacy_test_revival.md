# Nightly: Legacy-Test-Revival

Suche im Repository nach übersprungenen oder dauerhaft roten Tests
(`@pytest.mark.skip`, `@pytest.mark.xfail`, auskommentierte Test-Funktionen)
und reaktiviere **einen** davon pro Run:

1. Wähle den Test mit dem besten Verhältnis aus Aussagekraft zu Aufwand.
2. Repariere die Ursache im Produktionscode (innerhalb der erlaubten
   Surfaces), nicht den Test selbst — es sei denn, der Test prüft veraltetes
   Verhalten, dann passe ihn an das aktuelle Soll an und dokumentiere warum.
3. Entferne den Skip-/Xfail-Marker und stelle sicher, dass die gesamte
   Quick-Suite grün bleibt.

Wenn kein solcher Test existiert: melde "nothing more to do" und beende
dich, statt anderweitige Änderungen zu erfinden.
