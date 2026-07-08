================================================================
BESPRECHUNGSPROTOKOLL
Projekt:   Industriewäscherei Neubau – Simulationsstudie Ist-Zustand
Thema:     Planungsabstimmung MFS-Modell / Offene Punkte
Datum:     [geschwärzt]
Ort:       Besprechungsraum ILM, Gebäude [k.A.]
Dauer:     ca. 90 Minuten
================================================================
Teilnehmer:
  MM  – Marcel Müller (Projektleitung, ILM)
  JW  – Jonas Weiß (Simulation, ILM)
  SK  – Sabine Krause (Schichtleiterin, Betrieb) [per Telefon, ab Pkt. 3]
  [weitere TN nicht protokolliert]
================================================================
Protokollführer: JW | Status: ENTWURF – nicht von MM freigegeben
================================================================


TOP 1: Stand Simulationsmodell & Datenlage
------------------------------------------

JW berichtet über aktuellen Stand: Grundstruktur des Modells steht,
folgende Stationen sind angelegt:
  ✓ Wareneingang / Eingangspuffer
  ✓ LKW-Ankunft (2× täglich, Gleichverteilung)
  ✓ Sortierung & Losbildung (1 Los parallel, 1,5 s/Stück)
  ✓ Puffer vor Wascher
  ✓ Tunnelwascher (6 Kammern, 3 min/Kammer)
  ✓ Verteilung auf Nachverarbeitungspfade (Hemden / HT / TC+BL)
  ○ Finisher (in Arbeit – Tunnellänge fehlt noch)
  ○ Trockner (Verteilungsparameter noch nicht final)
  ✗ Falten & Stapeln noch nicht implementiert

Offene Modellierungsfragen → siehe TOP 3 und TOP 4.


TOP 2: Klärung Kammeranzahl Tunnelwascher
------------------------------------------

JW: "Im Lastenheft v1.2 steht unter Abschnitt 5.2 'Anzahl Kammern: 8'
– das wurde aus der Referenzabbildung übernommen. Im Text der
Aufgabenstellung steht aber klar 6 Kammern, und die Gesamtwaschzeit
von 18 Minuten passt nur bei 6×3 min. Was gilt?"

MM: "Eindeutig 6. Das Bild zeigt einen anderen Wascher-Typ mit 8
Kammern, das ist nur zur Illustration. Im Lastenheft ist das ein
Fehler – Jonas, bitte korrigieren auf 6 Kammern."

  → AKTION [JW]: Lastenheft v1.2, Abschnitt 5.2: Kammeranzahl von 8
    auf **6 Kammern** korrigieren. Offenen Punkt #7 schließen.

MM ergänzt: "Alle Berechnungen in DOC7 auf Basis 6 Kammern sind
damit korrekt. Die Gesamtwaschzeit bleibt 18 Minuten."


TOP 3: Strittiger Punkt – Stapelgröße Hemden (10 vs. 12)
----------------------------------------------------------

JW fasst Datenlage zusammen:
  - Prozessbeschreibung (Ursprungsdokument): 10 Hemden/Stapel
  - Betriebsleiter Bergmann (E-Mail): 12 Hemden/Stapel
  - Schichtleiterin Krause (Interview): 10 Hemden/Stapel
  - Lastenheft v1.2 Abschnitt 5.5: 12 Hemden/Stapel (aus Bergmann-Angabe)

MM: "Zwei gegen zwei. Wir nehmen die Ursprungsquelle als maßgeblich.
Für das Ist-Simulationsmodell setzen wir **10 Hemden pro Stapel**."

SK (telefonisch zugeschaltet): "Wir machen 12. Aber gut, wenn das
für das Modell so vorgesehen ist, dann ist das halt der Plan."

  → BESCHLUSS: Stapelgröße Hemden = **10 Stück** (für Ist-Modell).
    Sensitivitätsanalyse mit 12 Stück als Variante vorgesehen.
  → AKTION [JW]: Lastenheft v1.2, Abschnitt 5.5: auf 10 korrigieren.


TOP 4: Stochastische Parameter – Finale Festlegung
----------------------------------------------------

4a) Umrüstzeit Tunnelwascher:

  Datenlage:
    - Lastenheft v1.1: Gleichverteilung 5–10 min
    - DOC7 Kapazitätsrechnung: 7 min Fixwert (aus Schichtleiterangabe)
    - SK (Interview & E-Mail): 5–10 min Bandbreite
    - Bergmann (E-Mail): 7 min Fixwert

  MM: "Gleichverteilung 5–10 Minuten ist die robustere Annahme und
  entspricht dem Lastenheft. Wir übernehmen das für das Modell."

  SK: "Ja, das passt. 7 Minuten ist nur der Mittelwert aus dem Bauch."

  → BESCHLUSS: Umrüstzeit TWA-01 = **Gleichverteilt U(5 min, 10 min)**.
  → AKTION [JW]: DOC7 Randnotiz auflösen, Fixwert durch Verteilung ersetzen.

4b) Trockner-Programmdauer:

  Datenlage:
    - SK (E-Mail + Interview): drei Werte 15 / 20 / 30 Minuten
    - DOC7: nur 20 min (Fixwert, vereinfacht)
    - Lastenheft v1.2 Abschnitt 5.6: 20 min (Fixwert, vereinfacht)

  JW: "Frau Krause hat dreimal die Werte 15, 20 und 30 Minuten genannt.
  Das legt eine Dreiecksverteilung nahe: Min=15, Modus=20, Max=30."

  SK: "Klingt richtig. Die 30 Minuten kommen wirklich nur manchmal vor."

  MM: "Gut. Für das Modell: Dreiecksverteilung Tri(15, 20, 30) Minuten."

  → BESCHLUSS: Trockner Programmdauer = **Tri(15 min, 20 min, 30 min)**.
  → AKTION [JW]: DOC7 und Lastenheft entsprechend aktualisieren (Vermerk
    auf Dreiecksverteilung statt Fixwert 20 min).

4c) LKW-Ankunftszeiten:

  Keine Diskussion – als geklärt bestätigt:
  LKW 1: U(6:00, 11:00) | LKW 2: U(14:00, 17:00) | stetige Gleichverteilung.

4d) Losbildungsdauer:

  Keine Diskussion – als geklärt bestätigt: 1,5 s/Stück (deterministisch).


TOP 5: Taktzeit Mangelmaschine (28 s vs. 30 s)
-----------------------------------------------

JW: "Im Herstellerangebot v2.3 steht 28 s/Stück. Frau Krause nennt
aus Erfahrung 30 s. Was modellieren wir?"

MM: "Für das Ist-Modell nehmen wir den Erfahrungswert aus dem Betrieb:
**30 Sekunden pro Stück**. Das Herstellerangebot ist für die neue
Anlage relevant, nicht für den Ist-Zustand."

SK: "30 Sekunden, ja. Die Maschine schafft das theoretisch schneller,
aber in der Praxis läuft das auf 30 Sekunden raus."

  → BESCHLUSS: Taktzeit MNG-01 = **30 s/Stück** (Ist-Modell).
  → AKTION [JW]: Maschinenparameter-Tabelle DOC3 entsprechend kennzeichnen.
    Für Planungsmodell (Soll) gesondert prüfen mit 28 s.


TOP 6: Tunnellänge Finisher (FIN-01)
--------------------------------------

JW: "In allen Unterlagen fehlt die Tunnellänge. Ich habe in DOC7 mit
4 Metern gerechnet – das ist aber eine Annahme."

MM: "Wo kommt die 4 Meter Annahme her?"

JW: "Aus der ursprünglichen Aufgabenbeschreibung – da steht explizit
'Tunnellänge: 4 m'."

MM: "Ah, dann ist das kein Annahme sondern eine Vorgabe. Bitte in alle
Dokumente eintragen wo noch 'k.A.' steht."

  → AKTION [JW]: Tunnellänge FIN-01 = **4 m** in DOC1 und DOC3 nachtragen.
    Offenen Punkt #9 schließen.


TOP 7: Simulationsperiode & Läufe
-----------------------------------

MM: "Simulationszeitraum ist 10 Tage, 0:00–24:00 Uhr – das steht so
im Lastenheft. Zur Anzahl der Simulationsläufe: Wie viele plant ihr?"

JW: "Das hängt von der Varianz der Ergebnisse ab. Wir starten mit
10 Läufen und prüfen dann statistische Stabilität."

MM: "Gut. Das müssen wir im Bericht begründen können."

  → AKTION [JW]: Anzahl Simulationsläufe nach erster Auswertung begründen.


TOP 8: Sonstige offene Punkte & AOB
-------------------------------------

- Gewicht Handtuch: 720 g (DOC1 Tabelle) vs. 721 g (alle anderen Quellen)
  → BESCHLUSS: **721 g** – Tabellenfelder in DOC1 zu korrigieren.

- Lademengen LKW: nochmals bestätigt:
    LKW 1: 3.700 Hemden, 1.800 Handtücher, 500 Tischtücher, 1.200 Bettlaken
    LKW 2: 1.300 Handtücher, 2.300 Hemden, 1.400 Bettlaken
  → Entladereihenfolge = Reihenfolge der Aufzählung. In Modell abbilden.

- Puffer: alle unbegrenzt, FIFO – keine Änderungen.

- SK verlässt Gespräch um [Uhrzeit nicht notiert].


================================================================
OFFENE AKTIONEN (ZUSAMMENFASSUNG)
================================================================

Nr.  | Aktion                                              | Zuständig | Bis
-----|-----------------------------------------------------|-----------|------
A01  | Kammeranzahl in LH v1.2 auf 6 korrigieren          | JW        | sofort
A02  | Stapelgröße Hemden in LH v1.2 auf 10 korrigieren   | JW        | sofort
A03  | Umrüstzeit in DOC7: Fixwert → U(5,10) ersetzen     | JW        | sofort
A04  | Trockner in DOC7 + LH: 20 min → Tri(15,20,30) min  | JW        | sofort
A05  | MNG-01 in DOC3: 28 s als "Soll" kennzeichnen       | JW        | nächste Woche
A06  | Tunnellänge FIN-01: 4 m in DOC1 + DOC3 eintragen   | JW        | sofort
A07  | Gewicht Handtuch in DOC1: 720 g → 721 g            | JW        | sofort
A08  | Anzahl Simulationsläufe nach Erstauswertung begründen| JW       | [TBD]

================================================================
Nächste Besprechung: [TBD – wird per E-Mail terminiert]
Verteiler: MM, JW, SK, Bergmann
================================================================
