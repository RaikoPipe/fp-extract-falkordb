================================================================
KAPAZITÄTSRECHNUNG – ROHENTWURF
Industriewäscherei Neubau | Ist-Zustand Basis
Bearbeiter: [Initialen unleserlich] | Version: 0.3 (nicht abgestimmt!)
================================================================
ACHTUNG: Entwurf – Werte nicht für Planung freigegeben.
Grundlage: Lastenheft v1.1 (nicht v1.2! Abweichungen möglich)
================================================================


----------------------------------------------------------------
1. TAGESBEDARF WÄSCHE (Ist-Anlieferung pro Tag)
----------------------------------------------------------------

Wäschetyp     | LKW 1 [Stk.] | LKW 2 [Stk.] | Gesamt/Tag [Stk.] | Gesamt/Tag [kg]
--------------|--------------|--------------|-------------------|------------------
Hemden        | 3.700        | 2.300        | 6.000             | 1.344,0
Handtücher    | 1.800        | 1.300        | 3.100             | 2.235,1
Tischtücher   | 500          | –            | 500               | 487,5
Bettlaken     | 1.200        | 1.400        | 2.600             | 1.510,6
              |              |              |                   |
GESAMT        | 7.200        | 5.000        | 12.200            | 5.577,2

Gewichtsgrundlage:
  Hemd     = 0,224 kg    Handtuch  = 0,721 kg
  Tischtuch = 0,975 kg   Bettlaken = 0,581 kg

  >> Hinweis: Handtuch-Gewicht hier mit 0,721 kg angesetzt.
     Lastenheft v1.2 nennt 720 g – Differenz minimal, für Kapazitätsrechnung vernachlässigt.


----------------------------------------------------------------
2. LOSBILDUNG & SORTIERUNG
----------------------------------------------------------------

Losbildungsdauer = 1,5 s × Anzahl Stück pro Los

Wäschetyp   | Losgröße [Stk.] | Lose/Zyklus | Dauer/Los [s] | Dauer 6 Lose [s] | Dauer 6 Lose [min]
------------|-----------------|-------------|---------------|------------------|-------------------
Hemd        | 223             | 6           | 334,5         | 2.007,0          | 33,45
Handtuch    | 69              | 6           | 103,5         | 621,0            | 10,35
Tischtuch   | 51              | 6           | 76,5          | 459,0            | 7,65
Bettlaken   | 86              | 6           | 129,0         | 774,0            | 12,90

Gesamtdauer Losbildung pro vollständigem Zyklus (24 Lose):  3.861 s = 64,35 min

Anzahl benötigter Zyklen pro Tag:
  Hemden:     6.000 Stk. / (6 × 223) = 4,48 → 5 Zyklen (gerundet)   [= 30 Lose Hemden]
  Handtücher: 3.100 Stk. / (6 × 69)  = 7,49 → 8 Zyklen (gerundet)   [= 48 Lose]
  Tischtücher:  500 Stk. / (6 × 51)  = 1,63 → 2 Zyklen (gerundet)   [= 12 Lose]
  Bettlaken:  2.600 Stk. / (6 × 86)  = 5,04 → 6 Zyklen (gerundet)   [= 36 Lose]

  !! ACHTUNG: Zyklusanzahl je Typ ist NICHT identisch – Lossequenz führt zu
     unterschiedlichen Wartezeiten je Typ. Bottleneck-Analyse erforderlich. !!


----------------------------------------------------------------
3. TUNNELWASCHANLAGE (TWA-01)
----------------------------------------------------------------

Grundparameter (lt. Lastenheft v1.1):
  Kammern:              6
  Verweilzeit/Kammer:   3 min
  Gesamtwaschzeit:      18 min
  Max. Last/Kammer:     50 kg
  Umrüstzeit:           7 min  [Fixwert nach Schichtleiterangabe]

  [Randnotiz: "In v1.1 stand 5-10 min gleichverteilt – was gilt??"]

Durchsatz TWA-01 im stationären Betrieb (ohne Umrüsten):
  Takt = 3 min/Los (eine Kammer läuft durch, nächste wird beladen)
  → Throughput = 1 Los / 3 min = 20 Lose / Stunde

Waschkapazität pro Tag (2 Schichten à 8,5 h = 17 h Brutto):
  Brutto-Kapazität: 17 h × 60 min / 3 min/Los = 340 Lose/Tag (theoretisch)

Umrüstzeiten (Typwechsel im Zyklus: Hemd→Handtuch→Tischtuch→Bettlaken = 4 Wechsel/Zyklus):
  Annahme: 4 Umrüstvorgänge × 7 min = 28 min Nettoverlust pro Vollzyklus
  
  >> Pro Tag geschätzte Umrüstverluste (grob, abhängig von Zyklusanzahl): ca. 112–196 min
     [Berechnung: 4–7 Vollzyklen × 4 Wechsel × 7 min]

Netto-Loskapazität (Schätzung): ca. 300–320 Lose/Tag

Tatsächlicher Losbedarf:
  Hemden: 30 Lose | Handtücher: 48 Lose | Tischtücher: 12 Lose | Bettlaken: 36 Lose
  = 126 Lose/Tag gesamt

  >> TWA-01 scheint ausreichend dimensioniert (126 << 300).
     ABER: Reihenfolge-Constraints und Pufferaufbau können zu Engpässen führen!
     → Detailanalyse via Simulation erforderlich.


----------------------------------------------------------------
4. NACHVERARBEITUNG – KAPAZITÄTSABSCHÄTZUNG
----------------------------------------------------------------

>> 4.1 MANGELMASCHINE (MNG-01) – Tischtücher + Bettlaken

  Tagesvolumen: 500 + 2.600 = 3.100 Stück
  Taktzeit: 28 s/Stück, 2 Stück parallel → effektiv 14 s/Stück
  
  Netto-Bearbeitungszeit: 3.100 × 14 s = 43.400 s = 723,3 min = 12,1 h

  Verfügbare Zeit (2 Schichten): 17 h Brutto
  Auslastung MNG-01: 12,1 / 17 = 71,2%  → Kapazität ausreichend

  [Handschriftlich ergänzt: "Taktzeit 28 s aus Angebot – ABER Lastenheft nennt 30 s!
   Bei 30 s wäre Auslastung = (3.100×15 s)/17h = 76,5% – trotzdem OK. Klären!"]


>> 4.2 FINISHER (FIN-01) – Hemden

  Tagesvolumen: 6.000 Hemden
  Tunnellänge: k.A. → Berechnung nicht möglich
  Geschwindigkeit: 0,2 m/s → bei 4 m Tunnel: Durchlaufzeit = 4 / 0,2 = 20 s/Hemd
  Mindestabstand: 3 cm → Hemdenbreite (23 cm) + 3 cm = 26 cm pro Slot
  Max. Hemden im Tunnel gleichzeitig: 4 m / 0,26 m ≈ 15 Hemden
  
  Durchsatz FIN-01: 1 Hemd alle (0,26 m / 0,2 m/s) = 1,3 s → 2.769 Hemden/h
  Netto-Zeit für 6.000 Hemden: 6.000 / 2.769 = 2,17 h
  Auslastung (17 h): ~12,7%  → kein Engpass am Finisher selbst

  [Randnotiz: "Tunnellänge hier mit 4 m angenommen lt. Aufgabenstellung –
   Lieferantenangebot bitte eintragen sobald vorhanden"]


>> 4.3 FALTEN & STAPELN HEMDEN (FST-H-01)

  Stapelgröße: 10 Stück [ACHTUNG: Lastenheft nennt 12 Stück – TBD]
  Anzahl Stapel/Tag bei 10er: 6.000 / 10 = 600 Stapel
  Anzahl Stapel/Tag bei 12er: 6.000 / 12 = 500 Stapel
  
  Bearbeitungszeit: μ = 10 s, σ = 0,5 s pro Stapel
  Gesamtzeit (10er, μ): 600 × 10 s = 6.000 s = 100 min = 1,67 h
  Auslastung (17 h): 9,8%  → kein Engpass


>> 4.4 TROCKNER (TRO-01) – Handtücher

  Tagesvolumen: 3.100 Handtücher × 0,721 kg = 2.235,1 kg
  Trocknerkapazität: 60 kg pro Charge
  Programmdauer: 20 min  [NUR MODALWERT! Dreiecksverteilung 15/20/30 min – hier vereinfacht!]

  Anzahl Chargen: 2.235,1 kg / 60 kg = 37,3 → 38 Chargen/Tag
  Gesamtzeit Trockner: 38 × 20 min = 760 min = 12,67 h
  Auslastung (17 h): 74,5%

  [Fußnote: "Bei Verwendung der vollen Dreiecksverteilung (E[X]=21,67 min) wäre
   Gesamtzeit ≈ 38 × 21,67 = 823 min = 13,7 h → Auslastung 80,7%. Kritisch!
   Anzahl Trockner TBD – ggf. 2 Trockner erforderlich."]


>> 4.5 FALTEN & STAPELN HANDTÜCHER (FST-T-01)

  Stapelgröße: 5 Stück
  Anzahl Stapel/Tag: 3.100 / 5 = 620 Stapel
  Bearbeitungszeit: μ = 8 s, σ = 0,25 s pro Stapel
  Gesamtzeit (μ): 620 × 8 s = 4.960 s = 82,7 min = 1,38 h
  Auslastung (17 h): 8,1%  → kein Engpass


----------------------------------------------------------------
5. ZUSAMMENFASSUNG AUSLASTUNGSSCHÄTZUNG (GROB)
----------------------------------------------------------------

Aggregat              | Auslastung [%]  | Bewertung            | Annahmen
----------------------|-----------------|----------------------|----------------------------
Losbildung/Sortierung | [nicht ber.]    | Sequenz-Engpass?     | Simulation erforderlich
TWA-01 (Waschen)      | ~37–42%         | unkritisch (Kapaz.)  | Reihenfolge-Constraints!
MNG-01 (Mangeln)      | 71–77%          | moderat              | Taktzeit 28 vs. 30 s offen
FIN-01 (Finisher)     | ~13%            | unkritisch           | Tunnellänge angenommen
FST-H-01 (F&S Hemd)  | ~10%            | unkritisch           | Stapelgröße offen
TRO-01 (Trockner)     | 75–81%          | ⚠️ potenziell krit. | Nur 1 Trockner angenommen!
FST-T-01 (F&S HT)    | ~8%             | unkritisch           | –

FAZIT: Reine Kapazitätsrechnung zeigt keine offensichtlichen Engpässe außer möglicherweise
beim Trockner. ABER: Dynamische Effekte (Lossequenz, Pufferaufbau, Stochastik) nur via
Materialflusssimulation bewertbar. Diese Rechnung ist KEIN Ersatz für die Simulation!

================================================================
Nächster Schritt: Aufbau Simulationsmodell auf Basis dieser Daten.
Rückkopplung an Planungsteam nach erster Simulationsauswertung.
================================================================
