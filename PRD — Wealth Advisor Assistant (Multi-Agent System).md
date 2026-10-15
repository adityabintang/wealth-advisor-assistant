# PRD — Wealth Advisor Assistant (Multi-Agent System)

## 1. Ringkasan

Wealth Advisor Assistant adalah sistem multi-agent ringan yang membantu seorang wealth advisor menyiapkan keputusan advisory untuk klien. Sistem menerima data keuangan klien (JSON), melengkapinya dengan konteks dari CRM (mock API), menganalisis portofolio dan transaksi termasuk deteksi anomali, lalu menghasilkan output terstruktur (ringkasan, sinyal risiko, rekomendasi tindakan) yang bisa divalidasi advisor sebelum difinalisasi.

PRD ini ditulis untuk technical assignment AI Engineer (Agent Systems) dengan tenggat 24 jam. Scope sengaja dibatasi pada hal yang bisa berjalan end-to-end, dengan prioritas pada kejelasan arsitektur, modularitas agent, kualitas kode, dan penanganan edge case, sesuai kriteria evaluasi di dokumen assignment.

## 2. Masalah dan Peluang

- Advisor harus membaca banyak sumber (transaksi, catatan CRM, profil risiko) secara manual sebelum bertemu klien.
- Sinyal risiko seperti transaksi tak wajar, konsentrasi aset, atau arus keluar besar mudah terlewat.
- Hasil analisis biasanya tidak terstruktur sehingga sulit diaudit dan dibandingkan antar waktu.

Peluang: memisahkan tanggung jawab ke agent khusus yang dikoordinasikan orchestrator, membungkus semua akses data lewat tool abstraction, dan menghasilkan output terstruktur dengan jejak audit lengkap.

## 3. Tujuan, Non-Tujuan, dan Metrik

**Tujuan**

- Pipeline end-to-end: ingest data klien → fetch konteks CRM → analisis + anomaly detection → output advisory terstruktur.
- Arsitektur multi-agent yang jelas: 1 orchestrator + minimal 2 agent spesialis.
- Tool abstraction: agent tidak bergantung pada implementasi mentah.
- Sistem stabil saat gagal (API error, data hilang) dengan fallback yang terdokumentasi.
- Logging terstruktur untuk observability.
- Bonus: memory layer (short-term dan long-term) dan checkpoint human-in-the-loop (HITL).

**Non-tujuan**

- Eksekusi trading atau transaksi sungguhan.
- Autentikasi, KYC, atau integrasi CRM produksi.
- UI web produksi (CLI sudah cukup).
- Memberikan nasihat investasi final: keputusan akhir selalu di tangan advisor.

**Metrik keberhasilan**

| Metrik | Target |
| --- | --- |
| Run end-to-end pada semua sample client (termasuk edge case) | 100% selesai tanpa crash |
| Recall anomali yang sengaja disisipkan di data mock | ≥ 90% |
| Langkah eksekusi tercatat di log dengan run\_id | 100% |
| Perilaku saat CRM down | Degraded mode + fallback, bukan crash |
| Setup sampai berjalan dari repo bersih | ≤ 3 perintah |
| Waktu eksekusi per klien (mode offline) | < 10 detik |

## 4. Pengguna dan User Stories

**Persona utama:** Wealth Advisor yang menyiapkan review klien. **Persona sekunder:** reviewer teknis yang menilai desain dan kode.

- Sebagai advisor, saya ingin satu ringkasan kondisi keuangan klien agar tidak membuka banyak sumber.
- Sebagai advisor, saya ingin anomali ditandai dengan bukti dan tingkat keparahan agar bisa menindaklanjuti yang penting.
- Sebagai advisor, saya ingin bisa menyetujui, mengubah, atau menolak rekomendasi sebelum final.
- Sebagai advisor, saya ingin sistem tetap memberi hasil (dengan peringatan) ketika CRM tidak tersedia.
- Sebagai reviewer, saya ingin log yang menjelaskan siapa memutuskan apa dan kenapa.

## 5. Scope

**In scope (P0):** orchestrator, Data Fetcher Agent, Analyzer Agent, tool registry, mock CRM, anomaly detection, output terstruktur, error handling, logging, README, sample input/output.

**In scope (bonus, P1):** short-term memory, long-term memory, HITL checkpoint, Recommender Agent terpisah.

**Out of scope:** UI web, deployment cloud, data pasar real-time, multi-user, autentikasi.

## 6. Functional Requirements

| ID | Requirement | Prioritas |
| --- | --- | --- |
| FR-1 | Orchestrator menerima request (client\_id) dan membuat run\_id serta session context | P0 |
| FR-2 | Orchestrator menjalankan agent berurutan, mencatat status tiap langkah (pending, running, success, failed, skipped), dan menangani kegagalan per langkah | P0 |
| FR-3 | Data Fetcher memuat dan memvalidasi JSON data keuangan klien (profil, akun, portofolio, transaksi) | P0 |
| FR-4 | Data Fetcher memanggil CRM API (mock) via tool untuk konteks klien: profil risiko, catatan interaksi, tujuan, status | P0 |
| FR-5 | Analyzer menghitung metrik portofolio: total aset, alokasi per kelas aset, konsentrasi, arus kas 30/90 hari, perubahan nilai | P0 |
| FR-6 | Analyzer menjalankan anomaly detection (lihat bagian 10) dan menghasilkan daftar temuan dengan severity, bukti, dan penjelasan | P0 |
| FR-7 | Sistem menghasilkan AdvisoryOutput terstruktur (JSON tervalidasi skema) berisi ringkasan, temuan, rekomendasi tindakan, confidence, dan flag degraded | P0 |
| FR-8 | Semua akses eksternal (file, CRM, memori, LLM) lewat Tool interface | P0 |
| FR-9 | Error handling: retry, timeout, fallback, dan partial result (lihat bagian 11) | P0 |
| FR-10 | Logging terstruktur untuk alur eksekusi, keputusan, dan error dengan korelasi run\_id | P0 |
| FR-11 | Short-term memory: konteks sesi per run yang dibagi antar agent | P1 |
| FR-12 | Long-term memory: simpan ringkasan analisis dan keputusan advisor per klien, dipakai untuk membandingkan tren dan mengurangi alert berulang | P1 |
| FR-13 | HITL checkpoint sebelum finalisasi pada kondisi tertentu (lihat bagian 14) | P1 |
| FR-14 | CLI: jalankan untuk satu klien atau semua klien sample; setiap run menghasilkan dua output: JSON tervalidasi dan laporan teks untuk advisor | P0 |
| FR-15 | Narasi ringkasan oleh LLM bersifat opsional dan provider-agnostik (provider dan model dipilih lewat konfigurasi), dengan fallback template bila LLM tidak tersedia | P2 |

## 7. Arsitektur

**Alur tingkat tinggi**

```
CLI / Request
     │
     ▼
Orchestrator ──(session memory, logger, tool registry)
     │
     ├─1─► Data Fetcher Agent ──► ClientDataTool, CRMTool
     │         └─► ClientContext (+ data quality flags)
     │
     ├─2─► Analyzer Agent ──► PortfolioMetricsTool, AnomalyDetectionTool
     │         └─► AnalysisReport (metrics + anomalies)
     │
     ├─3─► Recommender Agent ──► (opsional LLM) / template
     │         └─► AdvisoryDraft
     │
     ├─4─► HITL Checkpoint (approve / edit / reject)
     │
     └─5─► Finalize ──► AdvisoryOutput + simpan ke long-term memory
```

**Komponen**

| Komponen | Tanggung jawab | Input → Output |
| --- | --- | --- |
| Orchestrator | Koordinator pusat (graph LangGraph): routing, urutan eksekusi, retry level langkah, keputusan degraded mode, HITL | request → AdvisoryOutput |
| Data Fetcher Agent | Ambil, validasi, normalisasi data dari file dan CRM | client\_id → ClientContext |
| Analyzer Agent | Metrik keuangan dan anomaly detection | ClientContext → AnalysisReport |
| Recommender Agent | Susun rekomendasi dan ringkasan dari temuan | AnalysisReport → AdvisoryDraft |
| Tool Registry | Daftar tool dengan interface seragam | name → Tool |
| Memory Store | Short-term (in-process) dan long-term (SQLite/JSON) | key → value |

**Keputusan desain utama**

- **Orchestrator berupa graph LangGraph (StateGraph) dengan routing deterministik.** Tiap agent menjadi node, dan transisi antar node (termasuk jalur degraded dan HITL) berupa conditional edge yang ditentukan kode, bukan LLM. Alasan: perilaku bisa diprediksi, mudah diuji dan di-log, serta state, checkpointer, dan interrupt untuk HITL sudah tersedia. Trade-off: kurang fleksibel dibanding routing dinamis oleh LLM.
- **LangGraph hanya sebagai lapisan orkestrasi.** Agent dan tool tetap class Python biasa yang tidak meng-import LangGraph; graph hanya menghubungkan mereka. Alasan: modularitas terjaga dan agent bisa diuji tanpa graph. Trade-off: tambahan dependensi dan kurva belajar, dan API LangGraph berubah cukup cepat sehingga versinya di-pin.
- **LLM bersifat agnostik.** Agent hanya mengenal antarmuka `LLMProvider`; implementasinya membungkus chat model LangChain sehingga OpenAI, Anthropic, Gemini, atau model lokal (Ollama) bisa dipakai cukup dengan mengganti konfigurasi (`LLM_PROVIDER`, `LLM_MODEL`, API key lewat env). Tidak ada provider default yang di-hardcode. Trade-off: fitur khusus satu provider tidak dipakai.
- **Anomaly detection berbasis aturan + statistik** (bukan model ML). Alasan: data mock kecil, hasil dapat dijelaskan, tidak butuh training. Trade-off: kurang adaptif terhadap pola baru.
- **LLM opsional** hanya untuk narasi. Analisis inti tetap deterministik agar sistem berjalan offline dan hasilnya reproducible; tanpa LLM terkonfigurasi, narasi memakai template.

## 8. Tool Abstraction

Setiap tool mengimplementasikan interface yang sama:

- `name` dan `description`
- `input_schema` (validasi input)
- `execute(input) → ToolResult` dengan field `ok`, `data`, `error`, dan `meta` (durasi, jumlah retry)

Agent memanggil tool lewat `ToolRegistry.get(name)`, tidak pernah meng-import implementasi mentah. Tool tidak melempar exception ke agent: kegagalan dikembalikan sebagai `ToolResult(ok=False, error=...)` agar penanganan error konsisten.

| Tool | Fungsi |
| --- | --- |
| ClientDataTool | Memuat dan memvalidasi JSON data keuangan klien |
| CRMTool | Memanggil mock CRM API (mendukung failure injection: timeout, 500, data kosong) |
| PortfolioMetricsTool | Menghitung alokasi, konsentrasi, arus kas, perubahan nilai |
| AnomalyDetectionTool | Menjalankan seluruh detektor anomali |
| MemoryTool | Baca/tulis memori jangka pendek dan panjang |
| NarrativeTool (opsional) | Membuat narasi via LLMProvider (provider apa pun yang dikonfigurasi), fallback ke template |

## 9. Model Data

**Client Financial Data (JSON)**

| Bagian | Field utama |
| --- | --- |
| client | client\_id, nama, tanggal lahir/usia, mata uang dasar |
| accounts | account\_id, tipe, saldo, mata uang |
| portfolio / holdings | asset\_id, kelas aset, sektor, kuantitas, nilai pasar, biaya perolehan |
| transactions | txn\_id, tanggal, jumlah, arah (in/out), kategori, counterparty, account\_id |
| history (opsional) | snapshot nilai portofolio 90 hari |

**CRM API (mock)**

| Field | Keterangan |
| --- | --- |
| risk\_profile | konservatif / moderat / agresif, plus batas alokasi |
| goals | tujuan keuangan dan horizon waktu |
| relationship | tier klien, tanggal kontak terakhir, advisor |
| notes | catatan interaksi terakhir |

**AdvisoryOutput (skema output)**

| Field | Keterangan |
| --- | --- |
| run\_id, client\_id, generated\_at | metadata |
| summary | ringkasan kondisi klien |
| metrics | metrik portofolio |
| anomalies\[\] | type, severity (low/medium/high), evidence, explanation |
| recommendations\[\] | action, rationale, priority, linked\_anomaly\_ids |
| confidence | 0–1, turun saat data tidak lengkap |
| degraded | true bila ada sumber data yang gagal, plus daftar alasan |
| review | status (approved / edited / rejected / pending\_review), reviewer note |

## 10. Spesifikasi Anomaly Detection

| Jenis | Metode | Ambang awal | Severity |
| --- | --- | --- | --- |
| Transaksi bernilai tidak wajar | Robust z-score (median/MAD) per kategori | > 3.5 atau > 5x median | Medium–High |
| Transaksi duplikat | Jumlah + counterparty sama dalam 24 jam | ≥ 2 kejadian | Medium |
| Lonjakan frekuensi | Transaksi harian vs rata-rata + 3σ | > mean + 3σ | Medium |
| Arus keluar besar | Net outflow 30 hari / total aset | > 30% | High |
| Counterparty baru bernilai tinggi | Counterparty pertama kali dengan nilai > ambang | Konfigurabel | Medium |
| Konsentrasi aset | Bobot satu aset atau sektor | > 40% | Medium |
| Menyimpang dari profil risiko | Alokasi ekuitas vs batas profil CRM | Melebihi batas | High |
| Tren penurunan nilai | Perubahan nilai portofolio 90 hari | < -10% | Medium |

Semua ambang disimpan di file konfigurasi. Setiap temuan wajib menyertakan bukti (txn\_id atau angka perhitungan) dan penjelasan singkat. Bila data kurang untuk metode statistik (misalnya < 10 transaksi), detektor statistik dilewati, aturan sederhana tetap jalan, dan confidence diturunkan.

## 11. Error Handling dan Fallback

| Kegagalan | Penanganan | Dampak pada hasil |
| --- | --- | --- |
| CRM timeout / 5xx | Retry 2x dengan exponential backoff; setelah itu circuit breaker; fallback ke konteks CRM terakhir di long-term memory, atau lanjut tanpa CRM | degraded = true, confidence turun |
| CRM data kosong / profil risiko tidak ada | Gunakan profil default konservatif dan tandai asumsi | Peringatan di output |
| JSON tidak valid | Fatal untuk klien tersebut; error jelas; run lain tetap jalan | Run gagal terkendali |
| Field opsional hilang | Validasi skema, field diisi default, flag data quality | Analisis parsial |
| Nilai janggal (negatif, tanggal tidak valid, mata uang campuran) | Normalisasi bila aman, jika tidak karantina baris dan catat | Baris dilewati, tercatat di log |
| Exception di dalam agent | Orchestrator menangkap, menandai langkah failed, lanjut bila langkah non-kritis | Partial result |
| LLM tidak tersedia | Fallback narasi berbasis template | Tanpa perubahan pada temuan |
| HITL tanpa respons (non-interaktif) | Status pending\_review, tidak pernah auto-approve diam-diam | Perlu review manual |

Prinsip: gagal secara terkendali, selalu ada hasil atau error yang jelas, dan setiap kegagalan terlihat di log dan di output (flag degraded).

## 12. Logging dan Observability

- Log terstruktur (JSON lines) ke file dan ringkasan ke console, dengan field: timestamp, level, run\_id, agent, step, event, duration\_ms, detail.
- Event wajib: run\_started, step\_started/finished, tool\_called (input ringkas, hasil, durasi, retry), decision (misalnya masuk degraded mode, memicu HITL), anomaly\_detected, error, run\_finished.
- Level: DEBUG (detail tool), INFO (alur), WARNING (fallback), ERROR (kegagalan).
- Data sensitif (nama lengkap, nomor akun) disamarkan di log.

## 13. Memory Layer (Bonus)

**Short-term:** konteks sesi per run (hasil antar langkah, flag data quality, keputusan sementara), hidup selama satu run, disimpan sebagai state graph LangGraph dan dibagi antar agent lewat Orchestrator.

**Long-term:** penyimpanan persisten (SQLite atau file JSON) berisi ringkasan analisis per klien, anomali sebelumnya, dan keputusan advisor. Dipakai untuk (a) membandingkan tren antar run, (b) menandai anomali yang berulang agar tidak mengulang alert yang sama, dan (c) fallback konteks CRM terakhir.

## 14. Human-in-the-Loop Checkpoint (Bonus)

Checkpoint dijalankan sebelum finalisasi bila salah satu terpenuhi: ada anomali severity High, confidence di bawah ambang, atau output dalam mode degraded.

- Advisor melihat ringkasan temuan dan rekomendasi di CLI, lalu memilih: **approve**, **edit** (mengubah atau menghapus rekomendasi), atau **reject**.
- Mode non-interaktif (`--auto-approve` untuk demo/CI) dicatat eksplisit di log dan di field review.
- Setiap override tercatat (siapa, kapan, perubahan apa) di log dan long-term memory; checkpoint memakai mekanisme interrupt dan resume LangGraph.

## 15. Persyaratan Non-Fungsional

- Modular: agent, tool, dan memori dapat diganti tanpa mengubah Orchestrator.
- Kode bersih: type hints, model data tervalidasi (Pydantic), docstring singkat, konfigurasi terpisah dari kode.
- Teruji: unit test untuk detektor anomali, tool, dan error path; satu integration test end-to-end.
- Reproducible: data mock dan failure injection dengan seed tetap.
- Aman: tidak ada secret di kode; `.env.example` disediakan; PII disamarkan di log.
- Mudah dijalankan: satu perintah setup dan satu perintah run.

## 16. Rekomendasi Tech Stack

| Kebutuhan | Pilihan |
| --- | --- |
| Bahasa | Python 3.11+ |
| Orkestrasi agent | LangGraph (StateGraph: node per agent, conditional edge, checkpointer, interrupt untuk HITL) |
| Abstraksi LLM | Antarmuka `LLMProvider` tipis di atas chat model LangChain (`init_chat_model`); provider dan model dipilih lewat konfigurasi, misalnya OpenAI, Anthropic, Gemini, atau Ollama |
| Validasi data | Pydantic v2 |
| CLI | Typer atau argparse, dengan output terminal memakai \`rich\` (tabel anomali berwarna, log ringkas) |
| HTTP / mock CRM | Kelas mock in-process, opsional FastAPI + httpx |
| Memori jangka pendek | State graph LangGraph + checkpointer in-memory |
| Memori jangka panjang | SQLite |
| Logging | `logging` standar dengan JSON formatter |
| Test | pytest |

## 17. Struktur Repository

```
wealth-advisor-assistant/
├── README.md
├── pyproject.toml
├── .env.example            # LLM_PROVIDER, LLM_MODEL, API key (opsional)
├── config/thresholds.yaml
├── data/
│   ├── clients/            # sample JSON (normal, anomali, data bolong, CRM down)
│   └── crm_mock.json
├── src/wealth_advisor/
│   ├── orchestrator.py     # graph LangGraph: state, node, edge
│   ├── agents/             # base.py, data_fetcher.py, analyzer.py, recommender.py
│   ├── tools/              # base.py, registry.py, client_data.py, crm.py, anomaly.py, metrics.py, memory.py
│   ├── llm/                # base.py (LLMProvider), factory.py
│   ├── memory/             # short_term.py, long_term.py
│   ├── models/             # schema Pydantic
│   ├── hitl.py
│   ├── logging_setup.py
│   └── cli.py
├── tests/
└── samples/output/         # contoh output tiap skenario
```

## 18. Deliverables

1. Repository GitHub berisi kode yang berjalan.
2. README (disarankan berbahasa Inggris, sesuai bahasa assignment) berisi: arsitektur, alur interaksi agent, keputusan dan trade-off, asumsi, dan instruksi menjalankan.
3. Sample input/output untuk skenario berikut:

| Skenario | Tujuan |
| --- | --- |
| Klien normal | Jalur sukses tanpa anomali |
| Klien dengan anomali tersisip | Validasi deteksi dan HITL |
| Data tidak lengkap / transaksi kosong | Partial analysis dan confidence turun |
| CRM down | Retry, fallback, mode degraded |
| JSON rusak | Error terkendali tanpa menghentikan run lain |

## 19. Rencana Kerja 24 Jam

| Jam | Pekerjaan |
| --- | --- |
| 0–2 | Finalisasi desain, skema data, struktur repo |
| 2–5 | Data mock, model Pydantic, tool interface dan registry |
| 5–9 | Data Fetcher, mock CRM + failure injection, error handling |
| 9–14 | Analyzer, metrik portofolio, detektor anomali + test |
| 14–17 | Orchestrator, Recommender, logging |
| 17–20 | Memory layer dan HITL |
| 20–23 | Integration test, sample output, README |
| 23–24 | Review akhir, jalankan dari repo bersih, submit |

Bila waktu mepet, potong dari bawah: Recommender dan NarrativeTool dulu, lalu long-term memory. Jangan potong error handling, logging, dan README.

## 20. Asumsi dan Risiko

**Asumsi**

- Data dan CRM boleh mock; tidak ada akses ke sistem produksi.
- Satu klien diproses per run; batch hanya iterasi sederhana.
- Satu mata uang dasar per klien; konversi kurs di luar scope (baris mata uang campuran dikarantina atau memakai kurs statis di konfigurasi).
- Ambang anomali bersifat contoh dan dapat dikonfigurasi, bukan standar regulasi.
- Output adalah alat bantu keputusan, bukan nasihat investasi.

**Risiko**

| Risiko | Mitigasi |
| --- | --- |
| Scope melebar karena bonus | Kerjakan P0 lebih dulu, bonus hanya setelah pipeline stabil |
| Over-engineering abstraksi | Interface minimal: Tool dan Agent masing-masing satu base class |
| Detektor menghasilkan false positive | Ambang dapat dikonfigurasi, severity bertingkat, bukti disertakan |
| Ketergantungan LLM merusak reproducibility | Analisis inti deterministik, LLM hanya narasi opsional |

## 21. Acceptance Criteria dan Pemetaan Evaluasi

| Kriteria evaluasi | Bukti di proyek |
| --- | --- |
| Desain sistem dan kejelasan arsitektur | Bagian 7 + diagram dan penjelasan alur di README |
| Abstraksi agent dan modularitas | Base class Agent/Tool, registry, agent tidak meng-import implementasi tool |
| Kualitas kode dan maintainability | Type hints, Pydantic, konfigurasi terpisah, test |
| Penanganan edge case | 5 skenario sample (bagian 18) + error matrix (bagian 11) |
| Ketelitian trade-off | Bagian keputusan dan trade-off di README |

Definisi selesai: semua requirement P0 terpenuhi, test lulus, lima skenario menghasilkan output atau error terkendali, dan proyek berjalan dari repo bersih mengikuti README.

## 22. Pertanyaan Terbuka

- Apakah penggunaan LLM (misalnya untuk narasi) diharapkan atau cukup sistem rule-based? Default PRD ini: opsional dan provider-agnostik, dengan mode offline.
- Apakah ada skema JSON data klien yang wajib diikuti? Jika ada, bagian 9 perlu disesuaikan.
