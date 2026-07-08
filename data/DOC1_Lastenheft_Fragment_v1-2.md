# Lastenheft – Industriewäscherei Neubau
**Dokumenttyp:** Anforderungsliste (Fragment)  
**Version:** 1.2  
**Stand:** [TBD – wird nach Abstimmung mit Betrieb ergänzt]  
**Ersteller:** Planungsgruppe ILM  
**Status:** ENTWURF – nicht freigegeben  

---

## 1. Betriebszeiten & Schichtmodell

| Parameter | Anforderung | Anmerkung |
|---|---|---|
| Schichten pro Tag | 2 | Frühschicht + Spätschicht |
| Frühschicht | 06:00 – 14:30 Uhr | inkl. 30 min Pause |
| Spätschicht | 14:00 – 22:30 Uhr | inkl. 30 min Pause |
| Überlappungszeit | [TBD] | Übergabe 14:00–14:30 zu klären |
| Simulationsbetrachtungszeitraum | 10 Tage, 0:00–24:00 Uhr | Basis für MFS-Modell |

---

## 2. Artikelspektrum

Die Anlage ist auf folgende vier repräsentative Wäschetypen auszulegen:

| Bezeichnung | Gewicht (g/Stück) | Abmessungen (cm) | Material | Farbe |
|---|---|---|---|---|
| Bettlaken | 581 | 180 × 290 × 0,8 | Polyester | Grau |
| Hemd | 224 | 23 × 30 × 0,2 | Baumwolle | Weiß |
| Handtuch | **720** | 35,6 × 69,8 × 1,2 | Baumwolle | Blau |
| Tischtuch | 975 | 240 × 120 × 0,1 | Synthetikfaser | Weiß |

> ⚠️ **Offener Punkt #3:** Gewichtsangabe Handtuch aus Lieferantenspezifikation zu bestätigen (interne Messung ergab 721 g).

---

## 3. Anlieferung & Wareneingang

### 3.1 Anlieferungsrhythmus
- **Anzahl LKW-Anlieferungen pro Tag:** 2 (Vormittag + Nachmittag)
- Entladezeit wird vernachlässigt (Annahme: instantan)

### 3.2 Ankunftszeiten (stochastisch)
| Lieferung | Verteilung | Zeitfenster |
|---|---|---|
| 1. LKW (Vormittag) | Gleichverteilung, stetig | 06:00 – 11:00 Uhr |
| 2. LKW (Nachmittag) | Gleichverteilung, stetig | 14:00 – 17:00 Uhr |

### 3.3 Ladungsmengen

**1. LKW – Ladereihenfolge (Entladung in dieser Reihenfolge):**

1. 3.700 Hemden  
2. 1.800 Handtücher  
3. 500 Tischtücher  
4. 1.200 Bettlaken  

**2. LKW – Ladereihenfolge:**

1. 1.300 Handtücher  
2. 2.300 Hemden  
3. 1.400 Bettlaken  

> **Hinweis Planung (intern, 14.05.):** Die Entladereihenfolge ist verbindlich und muss im Materialflusssimulationsmodell abgebildet werden. Steuerungslogik für Puffer im Wareneingang entsprechend auslegen.

---

## 4. Puffer (allgemein)

- **Kapazität:** unbegrenzt (alle Puffer)  
- **Steuerungsstrategie:** FIFO  
- **Stellfläche:** [TBD – Flächenprogramm noch nicht abgestimmt]

---

## 5. Prozessanforderungen

### 5.1 Sortierung & Losbildung

- Losbildung und Sortierung erfolgen sequenziell; es kann immer nur **ein Los gleichzeitig** gebildet werden.
- **Losbildungsdauer:** durchschnittlich **1,5 s pro Wäschestück**  
  *(Beispiel: 10 Teile → 15 s Losbildungszeit)*

**Lossequenz und Losgrößen (aktuell geplant):**

| Reihenfolge | Wäschetyp | Losgröße (Stück/Los) | Anzahl Lose pro Zyklus |
|---|---|---|---|
| 1 | Hemd | 223 | 6 |
| 2 | Handtuch | 69 | 6 |
| 3 | Tischtuch | 51 | 6 |
| 4 | Bettlaken | 86 | 6 |

> **Zykluslogik:** Nach Abschluss von 6 Losen Bettlaken beginnt der Zyklus erneut mit Hemden. Falls weniger als 6 volle Lose gebildet werden können (Puffer unzureichend befüllt), werden nur die verfügbaren Teilloses gebildet und gewaschen; anschließend folgt der nächste Wäschetyp.

---

### 5.2 Tunnelwaschmaschine (Batch-Wascher)

| Parameter | Wert | Quelle/Anmerkung |
|---|---|---|
| Anzahl Waschkammern | **8** | ⚠️ lt. Abbildung Referenzanlage – Herstellerbestätigung ausstehend |
| Max. Beladung pro Kammer | 50 kg | Technisches Datenblatt |
| Verweilzeit pro Kammer | 3 min/Los | Prozessvorgabe |
| Gesamtwaschzeit (alle Kammern) | 18 min | Berechnung: 6 Kammern × 3 min |
| Umrüstzeit (Typwechsel) | **ca. 7 min** | Erfahrungswert Schichtleiter |
| Betrieb | Ein Wäschetyp pro Durchlauf | Mischbeladung nicht zulässig |
| Belade-/Entladezeit | vernachlässigt | – |
| Transferzeit zwischen Kammern | in Verweilzeit enthalten | – |

> ⚠️ **Widerspruch intern:** Gesamtwaschzeit von 18 min basiert auf 6 Kammern, Kammeranzahl oben mit 8 angegeben – **vor Modellbau zu klären (Offener Punkt #7).**

---

### 5.3 Mangelmaschine (Mangeln, Falten & Stapeln – Tischtücher / Bettlaken)

| Parameter | Wert | Anmerkung |
|---|---|---|
| Taktzeit | **28 s/Stück** | lt. Herstellerangebot Mangle-Straße v2.3 |
| Parallele Verarbeitung | 2 Stück gleichzeitig | Bettlaken und Tischtücher gemeinsam |
| Zuführung | Puffer vorgelagert | FIFO |

> **Anmerkung Projektleitung:** Taktzeit 28 s aus Herstellerangebot; ältere Projektunterlagen nennen 30 s – maßgeblich ist das aktuelle Angebot. Rücksprache mit Lieferant bis [TBD].

---

### 5.4 Finisher (Hemden)

| Parameter | Wert | Anmerkung |
|---|---|---|
| Tunnellänge | [**k.A.**] | Lieferantenangabe ausstehend |
| Transportgeschwindigkeit | 0,2 m/s | Prozessvorgabe |
| Mindestabstand zwischen Hemden | 3 cm | Kollisionsschutz |
| Prinzip | Hakenförderersystem (kontinuierlich) | – |

---

### 5.5 Falten & Stapeln – Hemden

| Parameter | Wert |
|---|---|
| Stapelgröße (Auslöser) | **12 Stück** |
| Bearbeitungszeit pro Stapel | Normalverteilt: μ = 10 s, σ = 0,5 s |
| Modellierungshinweis | Keine explizite Stapeldarstellung – nur Wartelogik auf Vollständigkeit |

---

### 5.6 Trockner (Handtücher)

| Parameter | Wert | Anmerkung |
|---|---|---|
| Kapazität | 60 kg | – |
| Programmdauer | **20 min** | Standardprogramm (Annahme Planung) |
| Sonderhinweis | Alle Handtücher durchlaufen Trockenprogramm | auch bei Vortrocknung im Puffer |

---

### 5.7 Falten & Stapeln – Handtücher

| Parameter | Wert |
|---|---|
| Stapelgröße (Auslöser) | 5 Stück |
| Bearbeitungszeit pro Stapel | Normalverteilt: μ = 8 s, σ = 0,25 s |
| Modellierungshinweis | Keine explizite Stapeldarstellung – nur Wartelogik auf Vollständigkeit |

---

## 6. Offene Punkte (Auszug)

| Nr. | Beschreibung | Verantwortlich | Fällig |
|---|---|---|---|
| #3 | Gewicht Handtuch bestätigen (720 vs. 721 g) | Betrieb | TBD |
| #7 | Kammeranzahl Tunnelwascher klären (6 vs. 8) | Lieferant | TBD |
| #9 | Tunnellänge Finisher aus Angebot eintragen | Einkauf | TBD |
| #11 | Umrüstzeit Tunnelwascher – Verteilung oder Fixwert? | Simulation | TBD |
| #14 | Stapelgröße Hemden bestätigen (12 oder 10 Stück?) | Betrieb | TBD |
| #15 | Taktzeit Mangelmaschine final festlegen (28 s vs. 30 s) | Einkauf/Planung | TBD |
