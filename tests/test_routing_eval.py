"""Offline eval harness for the S1 complexity strategy.

~30 labeled prompts (mixed Italian/English) with a minimum tier-accuracy
gate, so keyword/weight tuning has a regression harness: change the defaults,
run this, see whether accuracy moved. Individual cases MAY be misclassified —
only the aggregate accuracy is asserted.
"""

from __future__ import annotations

from litestar_gateway.application.routing.complexity import ComplexityStrategy
from litestar_gateway.domain.routing import QualityTier

MIN_ACCURACY = 0.80

S, M, C, R = QualityTier.SIMPLE, QualityTier.MEDIUM, QualityTier.COMPLEX, QualityTier.REASONING

LABELED: list[tuple[str, QualityTier]] = [
    # ── SIMPLE ──
    ("What is the capital of France?", S),
    ("Ciao! Come stai?", S),
    ("Cos'è il PIL?", S),
    ("Define photosynthesis", S),
    ("Chi era Giuseppe Garibaldi?", S),
    ("How many continents are there?", S),
    ("Quanto costa un caffè a Milano?", S),
    ("Thanks, goodbye!", S),
    ("Traduci 'buongiorno' in inglese", S),
    # ── MEDIUM ──
    ("Write a python function that reverses a string", M),
    ("Scrivi una funzione che calcola la media di una lista", M),
    ("Fix this error: TypeError: 'NoneType' object is not iterable in my import", M),
    ("Come faccio il merge di un branch con git?", M),
    ("Write a SQL query that joins two tables and filters by date", M),
    ("Refactor this class to use dependency injection", M),
    ("Spiega la differenza tra una lista e una tupla in python con del codice", M),
    ("Summarize this article about the French Revolution in three paragraphs", M),
    # ── COMPLEX ──
    (
        "Design a scalable distributed architecture: implement the python api with "
        "authentication, encryption and low latency database queries",
        C,
    ),
    (
        "Progetta un'architettura distribuita e scalabile: implementa la funzione con "
        "autenticazione, crittografia e bassa latenza verso il database",
        C,
    ),
    (
        "Implement a rust async function with error handling that batches database "
        "queries, caches responses in memory and exposes an api endpoint with "
        "authentication and encryption, optimizing for throughput and latency",
        C,
    ),
    (
        "Debug this kubernetes deployment: the container crashes, the api endpoint "
        "returns errors, memory usage spikes and the database queries time out under "
        "concurrency; optimize the orchestration and the threading configuration",
        C,
    ),
    (
        "Ottimizza le prestazioni di questo microservizio: la latenza cresce con la "
        "concorrenza, la memoria satura e il protocollo http va in errore; implementa "
        "caching, ottimizzazione delle query al database e parallelismo",
        C,
    ),
    # ── REASONING ──
    ("Think through this step by step and explain your reasoning: the trolley problem", R),
    ("Ragiona passo dopo passo e valuta i pro e contro del nucleare in Italia", R),
    ("Evaluate the pros and cons of remote work, weigh the options and conclude", R),
    ("Analizza questo problema, scomponi le ipotesi e deduci la conclusione logica", R),
    ("Compare and contrast utilitarianism and deontology; show your work step by step", R),
    ("Let's think carefully: consider all the trade-offs and deduce the best option", R),
    ("Spiega il tuo ragionamento con una catena di pensiero: perché il cielo è blu?", R),
    ("Break down this argument step by step and infer whether it is logically valid", R),
]


def test_s1_tier_accuracy_meets_threshold() -> None:
    strategy = ComplexityStrategy()
    outcomes = [(prompt, expected, strategy.classify(prompt)[0]) for prompt, expected in LABELED]
    misses = [(p, e.value, got.value) for p, e, got in outcomes if got is not e]
    accuracy = 1 - len(misses) / len(outcomes)
    assert accuracy >= MIN_ACCURACY, (
        f"accuracy {accuracy:.2f} < {MIN_ACCURACY} — misses:\n"
        + "\n".join(f"  {e}→{g}: {p[:70]}" for p, e, g in misses)
    )
