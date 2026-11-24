# Shopdeck

Shopdeck is a second app for the deck, built from scratch. It takes a car from the trouble codes through the factory diagnostic chart to a quote, a parts list and the manual's repair steps, all on one printable work order.

It uses no language model. Everything on screen is one of three things:
- the manual's own text, with its page;
- a reading you typed, compared with the manual's limit by Python;
- your call, marked as yours.

The chat model is planned as a separate app.

## Run it

On the deck, run this once to put Shopdeck (with its icon) in the app list:

```
cd ~/open-diag-cyberdeck/shopdeck && ~/open-diag-cyberdeck/venv/bin/python -m shopdeck --install-icon
```

After that, open it from the app list like any other app. It opens in its own window.

To start it from a terminal instead:

```
cd ~/open-diag-cyberdeck/shopdeck && ~/open-diag-cyberdeck/venv/bin/python -m shopdeck
```

- **Over ssh:** add `--tui` to run the same screens inside the terminal.
- **Database:** it uses `~/open-diag-cyberdeck/kb/subaru.db` by default. Use `--kb /path/to/subaru.db` to point it at another file.
- **Car cable:** reading the car needs `pyserial`, and the window needs PyQt6. The deck's venv has both, which is why the commands above use it.
- **Where things are saved:** tickets go to `~/.shopdeck/tickets/`, one JSON file per car, saved after every change. Work orders go to `~/shopdeck_workorders/`.

## Keys

Everything works from the keyboard. The trackball is optional:
- the wheel scrolls;
- a left click picks a line or presses a button;
- a right click goes back.

In the window, the buttons along the bottom show each screen's keys, and F1–F8 open the eight screens from anywhere. The side buttons do the same.

| Where | Keys |
|---|---|
| Everywhere | arrows, PgUp and PgDn move; Enter picks; Esc or q goes back; the bottom line lists that screen's keys |
| Menu | 1–8 open the screens; n starts a new ticket; o opens an old one; s opens settings (rates); Q quits |
| Long lists (models, CHARM, search) | type to filter |
| Walking a chart | y or n answers; type a number to enter a reading; b goes back a step; g jumps to a step (noted as your call); m shows the manual pages; j turns the result into a job |
| Job | c adds CHARM book hours and part numbers; s attaches a manual procedure; h adds hours; p adds a part; d deletes a line |

## The screens

1. **Ticket & car.** Pick the model, year and engine, then the customer, VIN and mileage. "What the database has" shows how many of the car's charts can be walked.
2. **Customer interview.** These fields come from the factory "Check List for Interview". The full list is one key away.
3. **Codes & live data.** Type codes, or press r to read the car. Reading is read-only:
   - OBD-II modes 03, 07, 09 and 01;
   - Subaru SSM2 over the KKL cable, using only its read commands.

   Live data comes with general rules of thumb, labelled as such.
4. **Diagnose.** Each code opens its factory chart. Codes that share a chart, like several misfire codes, are walked once.
   - Steps that only ask which codes are showing are answered from the scan.
   - For a measurement step, type the reading and the deck compares it with the manual's limit.
   - The trail shows how each answer was decided: from the scan, measured, or you.
   - The diagnose screen also has the manual's symptom tables (for problems with no codes) and the Subaru service bulletins for each code.
5. **Your notes & theories.** This is for what the manual can't know, like a reused part, recent work or a hunch.
   - Note a reused part and the deck finds where the manual says to fit a new one, with the page. Example: a reused exhaust gasket finds "Replace gaskets with new ones" in Front Exhaust Pipe, p.283.
   - The manual never says what happens if you don't. So the deck shows where the rule is and lets you test the idea as your theory, kept separate from the manual's conclusions.
   - On a customer's car, the interview's "recent work" answer gets the same check.
6. **Jobs, parts & quote.** A finished chart, a note or your own words become a job. The job pulls in:
   - the manual procedure its <Ref.> points to;
   - CHARM book hours and part numbers;
   - the lines from the procedure that say a part must be new.

   The quote is computed in Python. Prices CHARM doesn't have are flagged, not guessed.
7. **Search the manual.** Search this car's factory manual and CHARM. Torque values are pulled out of the procedure text.
8. **Work order.** One HTML page to print or open in the browser. It contains:
   - the customer's report;
   - the diagnosis trail;
   - your notes;
   - the quote;
   - the parts list;
   - the factory repair steps, with "must be new" lines and torque values.

## Accuracy rules the code follows

- The deck never invents a step, a limit, a torque value or a price.
- A chart that didn't convert cleanly is never walked. If steps are missing, duplicated or cut short, the deck says so and shows the manual text with the page number.
- Limits are only parsed when a question has exactly one. Checks like "does it change when you shake the harness" stay with you.
- With several readings, every one must pass, and any that fail are listed.
- CHARM prices are labelled as old list prices.
- The chat app's scraped forum data is not used here.

## What the database can and can't do (found while building)

The app can only walk a chart that survived conversion from PDF. What it found, by manual, on Oct 19:

| Manual | Charts walkable | Notes |
|---|---|---|
| Forester 1999 | 104 of 105 | YES/NO are pictures in this PDF, so they're taken from the printed order when the labels are missing. Checked against the 2002 manual: 514 matching steps agree, none swapped. |
| Forester 2000, 2001 | 94 of 110, 89 of 104 | Same layout as 1999. The transmission charts are missing because these conversions are partial. |
| Forester 2002 | 83 of 86 | Clean conversion. |
| Impreza 2002 | 36 (2.5L), 21 (turbo) | Each engine has its own chapter, and the app now picks the right one. |
| Forester 2004, Legacy/Outback 2004 | about 34 | Many table cells were cut off mid-word in conversion. Re-converting with `kb/pdf_to_md.py` gave 185 (2.5L) and 190 (turbo) on the 2004 Forester in a test database. |
| Legacy/Outback 2005, 2008, 2009 | 18 to 30 | Headings scrambled and cells cut off. Re-converting with `kb/pdf_to_md.py` gave about 300 on the 2009 in a test database. |

- **Partly damaged charts:** a chart where only a step or two is damaged can still be walked. When you reach the damaged step, the app shows that step's text from the manual with the page number and lets you jump to the step it sends you to. It never guesses that step.
- **Badly damaged charts:** these are shown as manual text with the page number.
- **CHARM** covers every 1990–2012 Subaru with code descriptions, bulletins, book hours and part numbers. Its step-by-step charts are pictures, so for a car with only CHARM you get the code text and bulletins, not a walk.

## Tests

```
cd ~/open-diag-cyberdeck/shopdeck && python3 -m unittest discover -s tests
```

The tests check limit parsing, quote maths and tickets. With the database present, they also check:
- chart loading on a spread of models and years;
- that every walkable 2002 Forester chart leads to steps that exist;
- "any other DTC" answers;
- walks with readings;
- the reused-part lookup on several cars;
- torque extraction;
- a full work order.

## Files

| File | What it does |
|---|---|
| `kb.py` | reads the database (read-only) |
| `chart.py` | parses and walks the factory step tables |
| `repair.py` | procedures, torque, must-be-new lines, CHARM matching, quote |
| `case.py` | tickets |
| `link.py` | reads the car's cable |
| `report.py` | the work order |
| `desk.py` | the app's actions, without any screen code |
| `tui.py` | the screens (and the terminal version) |
| `gui.py` | draws the same screens in a window |
