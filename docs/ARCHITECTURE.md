# How the databases, the models and live data fit together

Three diagrams: how the databases are built (on the PC, once), what is inside a make's database, and what
happens on the deck every time you ask a question. GitHub draws them from the Mermaid text below.

The short version: **the models never answer from memory.** Python reads the car, judges every reading against
the manual's ranges and pulls the right pages out of SQLite. The model only gets that evidence to reason over,
and it has to cite it.

## 1. Building the databases (PC, offline)

```mermaid
flowchart LR
    subgraph SRC["Raw sources (downloaded by you, never in this repo)"]
        CHARM["CHARM / Operation CHARM<br/>service info, all makes"]
        FSM["Factory service manual PDFs<br/>(+ optional LEMON manuals)"]
        NHTSA["NHTSA: TSBs, complaints,<br/>recalls, investigations"]
        FORUM["Reddit + forum threads,<br/>Mechanics StackExchange"]
        RR["RomRaider / FreeSSM defs,<br/>OBD-II PID tables"]
        VPIC["NHTSA vPIC VIN data"]
        BOOKS["Public-domain repair books<br/>(Army/Navy manuals, EPA, Wikipedia)"]
    end

    subgraph BUILD["Builders (kb/, chevrolet/, card_baking/)"]
        B1["build_subaru_kb.py<br/>charm · fsm · diag · labor"]
        B2["add_nhtsa.py · add_investigations.py<br/>add_stackexchange.py"]
        B3["build_community.py<br/>extract fixes (local LLM),<br/>audit for credibility"]
        B4["bake_cards.sh<br/>one card per DTC"]
        B5["build_telemetry.py<br/>healthy zones from the manual"]
        B6["build_general_kb.py"]
        EMB["embed<br/>Qwen3-Embedding-0.6B<br/>int8 vectors"]
    end

    subgraph OUT["Finished databases (copied to the deck)"]
        MK[("kb/&lt;make&gt;.db<br/>one per make")]
        TH[("kb/&lt;make&gt;.threads.db<br/>raw community threads")]
        ID[("kb/vin.db · kb/romid.db<br/>vehicle ID")]
        GEN[("kb/general.db<br/>general repair books")]
    end

    CHARM --> B1
    FSM --> B1
    NHTSA --> B2
    FORUM --> B3
    B3 --> B4
    RR --> B5
    RR --> ID
    VPIC --> ID
    B1 --> MK
    B2 --> MK
    B3 --> MK
    B3 --> TH
    B4 --> MK
    B5 --> MK
    BOOKS --> B6 --> GEN
    EMB -. "vectors for search" .-> MK
    EMB -.-> TH
    EMB -.-> GEN
```

## 2. Inside one make's database (`kb/<make>.db`)

```mermaid
flowchart TB
    subgraph MANUAL["Manual layer (ranked first)"]
        S["section<br/>every manual page, with its path<br/>and pictures"]
        SV["section_vehicle<br/>which years/models a page covers"]
        DTC["dtc + dtc_procedure<br/>code definitions and the<br/>step-by-step diagnostic trees"]
        SP["spec<br/>torque, capacities, clearances"]
        LAB["labor<br/>book hours + OEM part numbers"]
    end
    subgraph TEL["Telemetry layer"]
        TP["telem_param + telem_source_map<br/>how each cable's raw value becomes<br/>one named reading"]
        TC["telem_condition<br/>idle, cruise, load, warm-up..."]
        TZ["telem_zone<br/>healthy range per condition,<br/>each row quoting its manual page"]
        TPA["telem_path<br/>where the manual sends you<br/>when a reading is out of range"]
    end
    subgraph COMM["Second-hand layer (ranked after the manual)"]
        CARD["dtc_card<br/>likely causes per code,<br/>counted from audited fixes"]
        KN["knowledge<br/>TSBs, recalls, complaints,<br/>confirmed forum fixes"]
    end
    subgraph SEARCH["Search indexes"]
        FTS["FTS5 keyword indexes<br/>(BM25)"]
        VEC["sqlite-vec tables<br/>embedding vectors"]
    end
    S --- SV
    S --- FTS
    S --- VEC
    SP --- FTS
    KN --- FTS
    KN --- VEC
    CARD --- FTS
    CARD --- VEC
    TZ --- TC
    TZ --- TPA
    TPA -. "points to" .-> S
```

## 3. Diagnosing on the deck (every question)

```mermaid
flowchart TB
    CAR(["Car"]) -- "KKL cable: Subaru SSM2<br/>OBDLink EX: OBD-II, GM mode 22" --> READ["READ CAR / TEST DRIVE<br/>codes + every supported live reading"]
    YOU(["You: symptoms, notes,<br/>question (typed or dictated)"]) --> Q["Question / case"]

    READ --> VIN["Vehicle ID<br/>VIN → vin.db / vPIC<br/>ECU ID → romid.db"]
    VIN --> PICK["Open kb/&lt;make&gt;.db<br/>scoped to year + model"]

    READ --> NORM["telemetry_ref.normalize<br/>raw bytes → named readings"]
    NORM --> COND["Work out the condition<br/>(idle / cruise / load...)"]
    COND --> JUDGE["judge: each reading vs telem_zone<br/>→ in / low / high + source"]
    PICK --> JUDGE
    JUDGE --> TSUM["Telemetry block<br/>out-of-range first, with where<br/>the manual points"]

    Q --> RET["Retriever (kb_retriever.py)<br/>FTS5 BM25 + vector search, fused"]
    PICK --> RET
    READ -- "DTCs" --> PROC["procedures.py<br/>the code's diagnostic tree,<br/>steps about codes answered in Python"]
    PICK --> PROC

    PROC --> CTX
    RET --> CTX["Case context, in priority order<br/>1 manual diagnostic tree<br/>2 baked card causes<br/>3 best other manual page<br/>4 confirmed community fixes ('ideas only')"]

    TSUM --> PROMPT
    CTX --> PROMPT["Prompt = case summary + live data judged<br/>+ earlier visits + evidence with sources<br/>+ rules: use only this, cite it"]
    HIST[("records/&lt;VIN&gt;.db<br/>this car's past readings,<br/>visits and chat")] -- "same readings last time" --> PROMPT

    PROMPT --> LLM{{"LLM<br/>deck NPU: Qwen3 1.7B / 4B<br/>or home PC: Qwen3.6 35B / Qwen3-Next 80B"}}
    LLM --> ANS["Answer: chat, Final Diagnosis,<br/>Repair Guide, with citations"]
    ANS --> PARTS["Parts: names from the repair line →<br/>OEM numbers from labor table → QR codes"]
    ANS --> HIST
    READ --> HIST

    Q -. "/fsm /torque /fluids:<br/>no model at all" .-> RAW["Manual text and spec rows<br/>straight from the DB"]
    PICK -.-> RAW
```

### What Python does vs. what the model does

| Python (exact, never a model) | The model (reasoning over what it's handed) |
|---|---|
| Decodes the cable's raw bytes into readings | Explains what the evidence means together |
| Works out the operating condition and judges each reading against the manual's range | Weighs your theory: what fits, what doesn't, quickest test |
| Answers diagnostic-tree steps that only ask which codes are set | Orders the likely causes and the next test |
| Finds the pages (keywords + embeddings) and puts the manual first | Writes the repair steps from the retrieved procedure |
| Looks up specs, torques and part numbers | Cites the page for every step and number |
| Compares today's readings with earlier visits | |
