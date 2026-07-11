# -*- coding: utf-8 -*-
# zkill_worker.py — Exécution en arrière-plan BORNÉE des lookups zKill.
#
# Pourquoi : chaque lookup réseau (ESI + zKill) peut prendre plusieurs
# secondes, et l'ancien code créait un thread PAR copie presse-papiers —
# ressources non bornées. Ce petit pool partagé (2 workers, 32 jobs en
# attente maximum) plafonne les threads et les requêtes simultanées tout en
# gardant la boucle d'événements Qt réactive.
from __future__ import annotations

from collections import deque
import logging
import threading
from typing import Callable

from .zkill_stats import fetch_pilot_stats

log = logging.getLogger(__name__)

ReadyCallback = Callable[[object], None]
ErrorCallback = Callable[[str], None]
Fetcher = Callable[[str, ReadyCallback, ErrorCallback], None]


class LookupJob:
    # Lookup annulable soumis au LookupPool.
    # Une requête HTTP déjà partie ne peut pas être tuée (limite de requests) :
    # l'annulation supprime la LIVRAISON du résultat, ce qui suffit quand la
    # carte destinataire a été fermée entre-temps.

    def __init__(
        self,
        name: str,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> None:
        # Mémorise les deux callbacks ; l'Event d'annulation est thread-safe
        # car lu par le worker et écrit par le thread UI.
        self.name = name
        self._on_ready = on_ready
        self._on_error = on_error
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        # Lecture de l'état d'annulation — utilisée par le worker avant le
        # fetch et avant chaque livraison.
        return self._cancelled.is_set()

    def cancel(self) -> None:
        # Supprime la livraison ; une requête HTTP déjà en cours va au bout
        # (2 max grâce au pool, donc sans danger pour les ressources).
        self._cancelled.set()

    def deliver_ready(self, value: object) -> None:
        # Livre le résultat, sauf si le job a été annulé entre-temps —
        # un callback tardif vers une carte fermée serait au mieux inutile.
        if not self.cancelled:
            self._deliver(self._on_ready, value, "ready")

    def deliver_error(self, message: str) -> None:
        # Même garde que deliver_ready, pour le chemin d'erreur.
        if not self.cancelled:
            self._deliver(self._on_error, message, "error")

    def _deliver(self, callback: Callable, value: object, kind: str) -> None:
        # Un récepteur Qt détruit ne doit JAMAIS tuer un worker du pool :
        # le callback est isolé pour que le thread survive et serve les
        # jobs suivants.
        try:
            callback(value)
        except Exception:
            log.exception(
                "zKill %s callback failed for %r", kind, self.name
            )


class LookupPool:
    # Pool fixe de threads daemon avec file d'attente bornée.
    # Daemon : les workers ne bloquent jamais la fermeture de l'app.
    # Borné : un réseau lent ne peut ni empiler des threads ni des jobs.

    def __init__(
        self,
        fetcher: Fetcher | None = None,
        max_workers: int = 2,
        max_pending: int = 32,
    ) -> None:
        # Le fetcher est injectable : les tests exercent le pool avec une
        # fonction synchrone, sans aucun accès réseau.
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")

        self._fetcher = fetcher or fetch_pilot_stats
        self._max_workers = max_workers
        self._max_pending = max_pending
        self._pending: deque[LookupJob] = deque()
        self._condition = threading.Condition()
        self._threads: list[threading.Thread] = []
        self._stopping = False

    def submit(
        self,
        name: str,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> LookupJob:
        # File un job et réveille un worker. Si la file déborde, le job EN
        # ATTENTE le plus ancien est annulé : l'utilisateur qui enchaîne les
        # copies ne s'intéresse plus aux vieux noms.
        job = LookupJob(name, on_ready, on_error)
        with self._condition:
            if self._stopping:
                job.cancel()
                return job

            self._start_workers_locked()
            if len(self._pending) >= self._max_pending:
                self._pending.popleft().cancel()
            self._pending.append(job)
            self._condition.notify()
        return job

    def shutdown(self, timeout: float = 2.0) -> None:
        # Annule le travail en attente et attend brièvement les workers actifs.
        # Utilisé par les tests ; en production les daemons meurent avec le
        # process, aucun arrêt explicite n'est nécessaire.
        with self._condition:
            self._stopping = True
            while self._pending:
                self._pending.popleft().cancel()
            self._condition.notify_all()

        for thread in tuple(self._threads):
            thread.join(timeout=timeout)

    def _start_workers_locked(self) -> None:
        # Démarrage paresseux : aucun thread tant que rien n'a été soumis
        # (le scan zKill peut ne jamais servir). Appelé sous _condition.
        if self._threads:
            return
        for index in range(self._max_workers):
            thread = threading.Thread(
                target=self._worker,
                name=f"zkill-lookup-{index + 1}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def _worker(self) -> None:
        # Boucle d'un worker : attendre un job, vérifier l'annulation AVANT le
        # fetch (jamais de réseau pour un job annulé), et ne jamais mourir sur
        # une exception — un pool à 2 threads ne survivrait pas à des morts
        # silencieuses.
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                job = self._pending.popleft()

            if job.cancelled:
                continue
            try:
                self._fetcher(
                    job.name,
                    job.deliver_ready,
                    job.deliver_error,
                )
            except Exception:
                log.exception("Unexpected zKill lookup worker failure for %r", job.name)
                job.deliver_error("NETWORK ERROR")


# Pool unique de l'application — partagé par toutes les cartes zKill pour que
# la borne (2 workers / 32 jobs) soit globale et pas par carte.
_default_pool = LookupPool()


def submit_lookup(
    name: str,
    on_ready: ReadyCallback,
    on_error: ErrorCallback,
) -> LookupJob:
    # Point d'entrée public : soumet un lookup au pool global borné et
    # retourne le job pour que l'appelant puisse l'annuler (fermeture carte).
    return _default_pool.submit(name, on_ready, on_error)
