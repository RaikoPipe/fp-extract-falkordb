================================================================
STÖRUNGSHISTORIE – REFERENZANLAGE (ANONYMISIERT)
Quelle: Betriebsdatenerfassung Referenzwäscherei [Name geschwärzt]
Zeitraum: 10 Betriebstage (identisch mit geplantem Simulationszeitraum)
Exportiert: [Datum geschwärzt] | Format: manuell bereinigter CSV-Export
================================================================
HINWEIS: Diese Daten stammen von einer Anlage mit strukturell
ähnlichem Prozessablauf. Maschinenparameter können abweichen.
Verwendung ausschließlich zu Planungszwecken.
Fehlende Felder = nicht erfasst oder unleserlich im Original.
================================================================


----------------------------------------------------------------
ABSCHNITT A: ROHDATEN STÖRUNGSEREIGNISSE
(Format: Lfd.Nr. | Tag | Schicht | Aggregat | Störungstyp |
 Beginn [hh:mm] | Ende [hh:mm] | Dauer [min] | Auswirkung | Bemerkung)
----------------------------------------------------------------

Lfd | Tag | Schicht | Aggregat        | Störungstyp              | Beginn | Ende  | Dauer | Auswirkung          | Bemerkung
----|-----|---------|-----------------|--------------------------|--------|-------|-------|---------------------|------------------------------------------
001 | 1   | F       | TWA-01          | Umrüstfehler Steuerung   | 07:14  | 07:26 | 12    | Stillstand Waschen  | Programmwechsel Hemd→Handtuch; Bediener-
    |     |         |                 |                          |        |       |       |                     | fehler; doppelt so lang wie normal
002 | 1   | S       | TRO-01          | Tür schließt nicht       | 16:03  | 16:18 | 15    | Stillstand Trockner | Dichtung verschlissen; manuell nachgeholfen
003 | 2   | F       | MNG-01          | Materialstau             | 09:41  | 09:53 | 12    | Stillstand Mangeln  | Tischtuch schief eingeführt; manuell beseitigt
004 | 2   | F       | TWA-01          | Überlastwarnung Kammer 3 | 10:22  | 10:28 | 6     | Kurzstopp           | Kammergewicht >50 kg; Los manuell reduziert
005 | 2   | S       | FIN-01          | Haken-Verklemmen         | 18:45  | 19:02 | 17    | Stillstand Finisher | Hemd verklemmt im Tunnel; Maschine gestoppt
006 | 3   | F       | FST-H-01        | Stapel unvollständig     | 08:17  | 08:21 | 4     | Kurzstopp F&S       | Zu wenig Hemden im Puffer; Warten auf 10 Stk.
007 | 3   | S       | TWA-01          | Umrüstfehler Steuerung   | 15:33  | 15:41 | 8     | Stillstand Waschen  | Programmwechsel Handtuch→Tischtuch; normal
008 | 3   | S       | TRO-01          | Überhitzung Sensor       | 20:11  | 20:44 | 33    | Stillstand Trockner | Temperatursensor Fehlmeldung; Programm-
    |     |         |                 |                          |        |       |       |                     | abbruch nach 20 min; Neustart erforderlich
009 | 4   | F       | MNG-01          | Materialstau             | 07:52  | 08:01 | 9     | Stillstand Mangeln  | Bettlaken gefaltet eingeführt; Bediener-
    |     |         |                 |                          |        |       |       |                     | schulung erforderlich
010 | 4   | F       | TWA-01          | Kammer leer, kein Nachsch| 11:03  | 11:31 | 28    | Leerstand TWA       | Puffer vor Wascher leer; Losbildung zu langsam
    |     |         |                 |                          |        |       |       |                     | (LKW-Verzögerung an dem Tag)
011 | 4   | S       | FIN-01          | Geschwindigkeitsabfall   | 17:29  | 17:35 | 6     | Teilausfall         | Antrieb kurzzeitig überhitzt; Selbstregelung
012 | 5   | F       | FST-T-01        | Stapel unvollständig     | 06:48  | 06:53 | 5     | Kurzstopp F&S       | Schichtbeginn; Puffer noch leer
013 | 5   | F       | TWA-01          | Umrüstfehler Steuerung   | 09:17  | 09:24 | 7     | Stillstand Waschen  | Programmwechsel Bettlaken→Hemd; normal
014 | 5   | S       | MNG-01          | Reinigungsstopp          | 21:00  | 21:20 | 20    | Geplanter Stopp     | Schichtende-Reinigung (nicht Störung!)
015 | 6   | F       | TRO-01          | Tür schließt nicht       | 08:33  | 08:41 | 8     | Stillstand Trockner | Gleiche Dichtung wie Tag 1; Provisorium
016 | 6   | S       | TWA-01          | Überlastwarnung Kammer 1 | 14:58  | 15:05 | 7     | Kurzstopp           | Los zu schwer; Handtuch-Los 69 Stk. × 0,721kg
    |     |         |                 |                          |        |       |       |                     | = 49,7 kg – Waage falsch kalibriert?
017 | 7   | F       | FST-H-01        | Stapel unvollständig     | 10:44  | 10:51 | 7     | Kurzstopp F&S       | Finisher-Ausgang hatte Rückstau
018 | 7   | S       | MNG-01          | Materialstau             | 19:23  | 19:37 | 14    | Stillstand Mangeln  | Tischtuch (240×120cm) – Einzug fehlerhaft
019 | 8   | F       | TWA-01          | Kammer leer, kein Nachsch| 08:11  | 08:49 | 38    | Leerstand TWA       | Losbildung verzögert durch späten LKW-1
020 | 8   | F       | FIN-01          | Haken-Verklemmen         | 11:02  | 11:13 | 11    | Stillstand Finisher | Hemd (23×30cm) an Naht eingehakt
021 | 8   | S       | TRO-01          | Kapazitätsüberschreitung | 16:37  | 16:42 | 5     | Kurzstop Trockner   | Charge >60kg befüllt; Sensor ausgelöst
022 | 9   | F       | TWA-01          | Umrüstfehler Steuerung   | 07:05  | 07:19 | 14    | Stillstand Waschen  | Programmwechsel Tischtuch→Bettlaken; lang
023 | 9   | S       | FST-T-01        | Stapel unvollständig     | 15:14  | 15:21 | 7     | Kurzstopp F&S       | Trockner-Ausgang Rückstau; Puffer leer
024 | 10  | F       | MNG-01          | Materialstau             | 09:08  | 09:17 | 9     | Stillstand Mangeln  | Bettlaken-Wechsel; Umstellproblem
025 | 10  | S       | TWA-01          | Geplante Revision        | 22:00  | 22:30 | 30    | Geplanter Stopp     | Routineprüfung Schichtende (nicht Störung!)


----------------------------------------------------------------
ABSCHNITT B: AUSWERTUNG NACH AGGREGAT
(automatisch aggregiert aus Rohdaten – nur ungeplante Störungen)
----------------------------------------------------------------

Aggregat  | Anz. Störungen | Gesamtausfall [min] | Ø Dauer [min] | Häufigster Störungstyp
----------|----------------|---------------------|----------------|------------------------
TWA-01    | 7 (+ 2 geplant)| 120                 | 17,1          | Umrüstfehler / Leerstand
TRO-01    | 4              | 61                  | 15,3          | Tür / Sensor / Überlast
MNG-01    | 4              | 44                  | 11,0          | Materialstau
FIN-01    | 3              | 34                  | 11,3          | Haken-Verklemmen
FST-H-01  | 2              | 11                  | 5,5           | Stapel unvollständig
FST-T-01  | 2              | 12                  | 6,0           | Stapel unvollständig

  GESAMT ungeplante Ausfälle: 22 Ereignisse | 282 min = 4,7 h über 10 Tage

  [Handschriftl. Anmerkung: "Leerstand TWA durch Losbildung ist kein
   Maschinenausfall! Eher Prozessmangel – in Simulation als Leerlauf
   modellieren, nicht als Störung." – unleserliche Unterschrift]


----------------------------------------------------------------
ABSCHNITT C: AUFFÄLLIGKEITEN & PLANUNGSHINWEISE
----------------------------------------------------------------

C.1 Tunnelwascher TWA-01 – Umrüstzeiten im Betrieb:

  Gemessene Umrüstdauern (nur Steuerungs-Umrüstungen, ungeplant):
  Tag 1: 12 min | Tag 3: 8 min | Tag 5: 7 min | Tag 9: 14 min | Tag 10: [geplant, n/a]
  
  Gemessene Umrüstdauern (Normalfälle, Ereignis-Nr. 007, 013):
  Tag 3: 8 min | Tag 5: 7 min

  Bandbreite Umrüstung gesamt: **6 – 14 min** (inkl. Fehlbedienung)
  Bandbreite ohne Fehlbedienung: **7 – 8 min**

  Planungshinweis: Im Simulationsmodell wurde U(5,10) min hinterlegt
  (lt. Protokoll DOC4). Referenzanlage zeigt etwas höhere Werte –
  ggf. konservativere Annahme prüfen.

C.2 Trockner TRO-01 – Programmdauer:

  Aus Ereignis 008: Programm lief 20 min bis Abbruch durch Sensor;
  Neustart und Restlaufzeit nicht erfasst.
  Aus Ereignis 002: 15 min Ausfall, danach Programm weitergelaufen.
  
  Programmzeiten laut Schichtleitung der Referenzanlage:
  "Wir fahren 15, 20 oder 30 Minuten – je nach Charge."
  → Dreiecksverteilung Tri(15, 20, 30) min plausibel bestätigt.

C.3 Leerstand Tunnelwascher (Ereignisse 010, 019):

  Beide Leerstandsereignisse (28 min, 38 min) traten bei verzögerter
  LKW-Ankunft auf. Losbildung konnte nicht schnell genug aufholen.
  → Systemisches Problem: Abhängigkeit TWA ↔ Losbildung ↔ LKW-Takt
  → In Simulation durch stochastische LKW-Ankunft automatisch abgebildet.

C.4 Kapazitätsgrenze Kammer (Ereignis 004, 016):

  Handtuch-Los: 69 Stk. × 0,721 kg = 49,749 kg → nahe 50-kg-Limit
  Bei Waagenungenauigkeit (Ereignis 016): Überlastwarnung ausgelöst
  → Losgrößen sind sehr nahe an der Kapazitätsgrenze ausgelegt.
  → Robustheitsproblem: geringe Reserve bei Gewichtsschwankungen.


----------------------------------------------------------------
ABSCHNITT D: NICHT AUSWERTBARE EINTRÄGE
----------------------------------------------------------------

Folgende Störungseinträge waren im Original unleserlich oder lückenhaft
und wurden aus der Auswertung entfernt:

  - Tag 6, Frühschicht: Störung FST-H-01, Dauer unbekannt (Feld leer)
  - Tag 9, Frühschicht: Störung [Aggregat unleserlich], ca. 5 min
  - Tag 10, Spätschicht: Doppelter Eintrag TWA-01, Duplikat entfernt

================================================================
Datenquelle: Referenzanlage (anonymisiert) | Interne Verwendung ILM
Nicht für externe Weitergabe bestimmt.
================================================================
