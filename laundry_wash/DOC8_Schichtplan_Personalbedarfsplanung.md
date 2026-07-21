================================================================
SCHICHTPLAN & PERSONALBEDARFSPLANUNG
Projekt: Industriewäscherei Neubau | Ist-Zustand (Planungsgrundlage)
Stand: Entwurf – nicht abgestimmt mit Betrieb
Ersteller: [TBD]
================================================================
DATENGRUNDLAGE: Schichtzeiten aus Lastenheft v1.2;
Prozesszuordnungen aus Aufgabenstellung;
MA-Anzahl: [weitgehend TBD – nicht in Aufgabenstellung spezifiziert]
================================================================


----------------------------------------------------------------
ABSCHNITT 1: SCHICHTMODELL (GRUNDSTRUKTUR)
----------------------------------------------------------------

Schicht       | Bezeichnung  | Beginn  | Ende    | Dauer netto | Überlappung
--------------|--------------|---------|---------|-------------|-------------------
Frühschicht   | FS           | 06:00   | 14:30   | 8,0 h       | 14:00–14:30 (30 min)
Spätschicht   | SS           | 14:00   | 22:30   | 8,0 h       | 14:00–14:30 (30 min)

Überlappungszeit (14:00–14:30 Uhr):
  - Beide Schichten anwesend (Übergabe, Einweisung)
  - 2. LKW kann in diesem Zeitfenster ankommen [LKW-2: U(14:00, 17:00)]
  - Übergabe-Prozedur: [TBD – nicht definiert]

Anmerkung: Pausenzeiten nicht gesondert ausgewiesen – in Schichtdauer
enthalten. Brutto-Schichtlänge = 8,5 h je Schicht.

Betriebszeit gesamt (2 Schichten):
  Brutto: 06:00–22:30 = 16,5 h/Tag
  Netto (ohne Überlappung): 06:00–14:30 + 14:30–22:30 = 16,0 h
  [Anmerkung: Simulationszeitraum 0:00–24:00 Uhr – Zeiten außerhalb
   der Schichten: kein aktiver Betrieb, aber Puffer läuft weiter]


----------------------------------------------------------------
ABSCHNITT 2: PROZESS-SCHICHT-ZUORDNUNG
----------------------------------------------------------------

Prozessschritt              | Agg.-ID    | FS  | SS  | MA-Bedarf  | Anmerkung
----------------------------|------------|-----|-----|------------|---------------------------
Wareneingang / Entladung    | WE-Puffer  | ✓   | ✓   | [TBD]      | LKW-1: FS | LKW-2: SS (ca.)
Sortierung & Losbildung     | –          | ✓   | ✓   | [TBD]      | 1,5 s/Stück; 1 Los parallel
Tunnelwascher Bedienung     | TWA-01     | ✓   | ✓   | [TBD]      | inkl. Umrüstung U(5,10 min)
Mangelstraße                | MNG-01     | ✓   | ✓   | [TBD]      | 2 Stück parallel, 30 s/Stk.
Finisher Hemden             | FIN-01     | ✓   | ✓   | [TBD]      | weitgehend automatisch
Falten & Stapeln Hemden     | FST-H-01   | ✓   | ✓   | [TBD]      | 10 Stk./Stapel, μ=10 s
Trockner Handtücher         | TRO-01     | ✓   | ✓   | [TBD]      | 60 kg/Charge; Tri(15,20,30)
Falten & Stapeln Handtücher | FST-T-01   | ✓   | ✓   | [TBD]      | 5 Stk./Stapel, μ=8 s
Versand / Ausgangspuffer    | –          | ✓   | ✓   | [TBD]      | –

  !! ALLE MA-ZAHLEN AUSSTEHEND – nicht in Aufgabenstellung spezifiziert !!
  !! Rückfrage an Betrieb / Lastenheft-Ergänzung erforderlich           !!


----------------------------------------------------------------
ABSCHNITT 3: TAGESABLAUF (EXEMPLARISCH, TAG 1 – FS)
----------------------------------------------------------------

Zeitpunkt    | Ereignis                                    | Prozessschritt
-------------|---------------------------------------------|---------------------------
06:00        | Frühschicht-Beginn                          | alle Stationen
06:00–11:00  | LKW-1-Ankunft (Gleichverteilt)              | Wareneingang
  → LKW-1 Ladung (Entladereihenfolge):
    1. 3.700 Hemden
    2. 1.800 Handtücher
    3.   500 Tischtücher
    4. 1.200 Bettlaken
  → nach Entladung: Wäsche in WE-Puffer (FIFO)

[nach LKW-1]  Losbildung startet: Hemden zuerst (6 Lose à 223 Stk.)
              Losbildungsdauer: 6 × 223 × 1,5 s = 2.007 s ≈ 33 min
              Danach: Handtuch-Lose (6 × 69 × 1,5 s = 621 s ≈ 10 min)
              Danach: Tischtuch-Lose (6 × 51 × 1,5 s = 459 s ≈ 8 min)
              Danach: Bettlaken-Lose (6 × 86 × 1,5 s = 774 s ≈ 13 min)

[parallel]    Tunnelwascher nimmt Hemden-Lose auf (sobald 1. Los fertig)
              TWA läuft: 6 Kammern × 3 min = 18 min Durchlaufzeit

14:00        | Schichtübergabe beginnt (30 min Überlappung) | alle Stationen
14:00–17:00  | LKW-2-Ankunft (Gleichverteilt)              | Wareneingang
  → LKW-2 Ladung (Entladereihenfolge):
    1. 1.300 Handtücher
    2. 2.300 Hemden
    3. 1.400 Bettlaken
  → nach Entladung: Wäsche in WE-Puffer (FIFO, hinter LKW-1-Ware)

14:30        | Frühschicht-Ende                            | alle Stationen
14:30        | Spätschicht übernimmt                       | alle Stationen
22:30        | Spätschicht-Ende                            | alle Stationen
22:30–06:00  | Keine aktive Schicht                        | –


----------------------------------------------------------------
ABSCHNITT 4: SCHNITTSTELLEN & OFFENE PUNKTE
----------------------------------------------------------------

Nr.  | Offener Punkt                                        | Dringlichkeit
-----|------------------------------------------------------|---------------
S-01 | MA-Bedarf je Prozessschritt (alle TBD)               | HOCH
S-02 | Übergabe-Prozedur bei Schichtwechsel 14:00–14:30    | MITTEL
S-03 | Verhalten bei LKW-2-Ankunft während Übergabe        | MITTEL
S-04 | Pausenregelung (wann, wie lang, Vertretung)          | NIEDRIG
S-05 | Nachtschicht? (0:00–6:00 kein Betrieb lt. Modell)   | NIEDRIG
S-06 | Reinigungszeiten (s. Störungshistorie: 20–30 min/Tag)| NIEDRIG

Hinweis für Simulation: Schichtgrenzen und Übergabe-Logik sind im
Simulationsmodell abzubilden, soweit sie Prozessunterbrechungen
verursachen. MA-Anzahl ist für Ist-Modell (ohne Ressourcen-
beschränkung) nicht zwingend erforderlich.

================================================================
[Letzte Seite – abgerissener Zettel, angeheftet:]

"Jonas – vergiss nicht dass der 2. LKW zwischen 14 und 17 Uhr kommt,
also MITTEN in den Schichtwechsel rein kann. Das gibt bei uns immer
Chaos wenn der pünktlich um 14 ankommt und alle gerade mit Übergabe
beschäftigt sind. Vielleicht ist das auch ein Punkt für die Simulation?
– SK"
================================================================
