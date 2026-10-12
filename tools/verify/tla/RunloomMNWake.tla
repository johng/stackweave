--------------------------- MODULE RunloomMNWake ---------------------------
(***************************************************************************)
(* TOOLING NOTE, not part of the model.  If TLC just reported a parse       *)
(* failure for THIS module -- "Module-Table lookup failure for module name  *)
(* X derived from X file name", or a tla2sany AbortException -- suspect the *)
(* toolchain before the spec.  Concurrent TLC JVMs sharing /tmp clobber the *)
(* standard modules SANY extracts from the jar under fixed names            *)
(* (tlaplus/tlaplus issue 688; fixed on master, in no release as of 1.7.4). *)
(* Our runners pass -Djava.io.tmpdir per JVM.  Full account: run_tla.sh. *)
(***************************************************************************)
(***************************************************************************)
(* TLA+ model of runloom's M:N HUB-SUBMIT protocol: a fiber published on a  *)
(* hub's sub-list (runloom_mn_hub_submit) is eventually drained and run,    *)
(* even when every kick sent to that hub is lost.  Since migration became   *)
(* the only mode, hub_submit carries FRESH fibers only (spawns,             *)
(* runloom_mn_fiber_core); a woken fiber goes through the wake_state        *)
(* machine to a deque or the global run-queue instead (wake_state.pml).     *)
(* The names below (Waker*, fp_inflight, "parked") are from when this route *)
(* also carried wakes: read "waker" as the spawner, "parked" as             *)
(* spawned-not-yet-run, and fp_inflight as the run's pending count.         *)
(* hub_fanout.pml proves the route COMPOSITION (some kick reaches every     *)
(* wait mode); this model proves the timed-wait BACKSTOP when they are all  *)
(* lost.  It is the SIBLING of RunloomWake.tla: same skeleton, a different  *)
(* state space because a hub differs from the single-thread drain on the    *)
(* axes the single-thread model cannot reach.                               *)
(*                                                                          *)
(* WHY A SEPARATE MODEL.  RunloomWake models ONE owner thread that re-peeks *)
(* a single shared wake_list and -- absent the f214341 fix -- blocks        *)
(* UNBOUNDED in epoll_wait(-1); its hazard is the unbounded block and its   *)
(* heal is a 2ms decide-to-block cap.  An M:N hub is structurally           *)
(* different:                                                               *)
(*   (1) DESTINATION is a PER-HUB sub-list h->sub_head, OWNER-DRAINED: only *)
(*       the target hub drains it, so a lost signal cannot be healed by any *)
(*       other thread re-peeking.                                           *)
(*   (2) The block is NEVER epoll_wait(-1): every idle wait is TIMED        *)
(*       (pump_ns ~1ms base; idle_cond cond_timedwait_ns).  On each timeout *)
(*       the hub re-loops and UNCONDITIONALLY re-drains sub_head.  So the   *)
(*       heal is the BOUNDED POLL, intrinsic to the timed wait.             *)
(*   (3) The SIGNAL is independent free-delivery kicks: the per-hub         *)
(*       idle_cond signal AND the wake-pump eventfd / self-pipe (plus the   *)
(*       io_uring loop eventfd, folded in here).  EITHER delivered heals at *)
(*       once; the lasso needs ALL lost.                                    *)
(*   (4) The in_sub_queue 0->1 CAS (a second publish of the same g is a     *)
(*       no-op) is abstracted as "append at most once", which the per-g     *)
(*       waker_pc already enforces.                                         *)
(*                                                                          *)
(* THE HAZARD.  The hub drains sub_head, finds it empty, and commits to a   *)
(* TIMED block.  In the instant between its empty re-check and the block, a *)
(* spawner appends g to sub_head (RELEASE), bumps sub_gen, and fires the    *)
(* kicks.  Each kick can be LOST for a low-level reason the hub cannot      *)
(* prevent at this layer: the wake-pump eventfd dedup (wake_pending 0->1    *)
(* exchange swallows a second poke), an idle_cond signal arriving before    *)
(* idle_waiting=1 is published, or a memory re-order of the kick vs the     *)
(* block.  We ABSTRACT each as nondeterministic free delivery; the fidelity *)
(* claim is only that a kick CAN be lost, not why -- all the backstop       *)
(* needs.                                                                   *)
(*                                                                          *)
(* THE FIX (what we are proving).  BoundedPoll=TRUE (RunloomMNWake.cfg):    *)
(* the idle wait is TIMED, so even with BOTH kicks lost the hub re-loops    *)
(* within one bounded poll tick and re-drains sub_head -> every appended    *)
(* fiber is eventually run; AllWoken HOLDS.  BoundedPoll=FALSE              *)
(* (RunloomMNWake_bug.cfg): the regression where an idle hub blocks         *)
(* UNBOUNDED (a backend with no usable per-hub eventfd that fell back to an *)
(* uninterruptible infinite wait, or a change that dropped the timed cap)   *)
(* -- with both kicks lost the appended g is stranded on sub_head forever   *)
(* while its hub sleeps -> AllWoken violated, the M:N lost-wakeup lasso.    *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Gs,         \* set of fiber ids; each published by its own spawner ("waker")
          BoundedPoll \* TRUE -> the idle hub's wait is TIMED (re-drains sub_head);
                      \* FALSE -> the regression: an idle hub blocks unbounded (bug)

VARIABLES
    sub_list,      \* SUBSET Gs : g's appended to their owner hub's sub_head, not yet drained
    idle_cond,     \* BOOLEAN : the per-hub idle_cond signal is pending (free delivery)
    pump_efd,      \* BOOLEAN : the per-hub wake-pump eventfd/self-pipe is pending (free)
    fp_inflight,   \* Nat : published fibers not yet run to completion (the run's pending)
    hub_pc,        \* the owner hub's main-loop program counter
    hub_timeout,   \* "pump_1ms" (TIMED -- re-drains) or "infinite" (the BoundedPoll=FALSE bug)
    fiber_pc,      \* [Gs -> {"parked","on_sub_list","ready","resumed"}]
    waker_pc       \* [Gs -> {"running","appended","signalled","done"}]  (waker for g)

vars == <<sub_list, idle_cond, pump_efd, fp_inflight, hub_pc, hub_timeout,
          fiber_pc, waker_pc>>

HubPCs == {"loop_top","drained_empty","decided","blocked"}

TypeOK ==
    /\ sub_list \subseteq Gs
    /\ idle_cond \in BOOLEAN
    /\ pump_efd \in BOOLEAN
    /\ fp_inflight \in 0..Cardinality(Gs)
    /\ hub_pc \in HubPCs
    /\ hub_timeout \in {"pump_1ms","infinite"}
    /\ fiber_pc \in [Gs -> {"parked","on_sub_list","ready","resumed"}]
    /\ waker_pc \in [Gs -> {"running","appended","signalled","done"}]

Init ==
    /\ sub_list = {}
    /\ idle_cond = FALSE
    /\ pump_efd = FALSE
    /\ fp_inflight = Cardinality(Gs)       \* each spawn counted pending before publish
    /\ hub_pc = "loop_top"
    /\ hub_timeout = "pump_1ms"
    /\ fiber_pc = [g \in Gs |-> "parked"]
    /\ waker_pc = [g \in Gs |-> "running"]

HasReady == \E g \in Gs : fiber_pc[g] = "ready"
AnyKick  == idle_cond \/ pump_efd          \* either free-delivery signal pending

----------------------------------------------------------------------------
\* ---- Spawner (one per fiber g): runloom_mn_fiber_core -> runloom_mn_hub_submit ----

\* L2 step 1: append g to the target hub's sub_head with a RELEASE store -- the
\* DURABLE publish, BEFORE the signal (runloom_mn_hub_submit).  The in_sub_queue
\* 0->1 CAS is abstracted as "at most once per g", which waker_pc="running"
\* already enforces.
WakerAppend(g) ==
    /\ waker_pc[g] = "running"
    /\ waker_pc' = [waker_pc EXCEPT ![g] = "appended"]
    /\ sub_list' = sub_list \cup {g}
    /\ fiber_pc' = [fiber_pc EXCEPT ![g] = "on_sub_list"]
    /\ UNCHANGED <<idle_cond, pump_efd, fp_inflight, hub_pc, hub_timeout>>

\* L2 step 2: fire the independent kicks.  sub_gen is bumped RELEASE first, then
\* the idle_cond signal (if the hub announced idle_waiting) and the
\* unconditional wake-pump kick are sent (runloom_mn_hub_submit).  Each has FREE
\* delivery:
\* idle_cond' may stay or become TRUE, pump_efd' may stay or become TRUE -- the
\* abstracted hazard that a kick CAN be lost (dedup-suppressed / re-ordered /
\* signalled before idle_waiting=1).  The g is already durably on sub_list, so a
\* delivered kick and a lost-but-re-polled kick both heal; both lost + unbounded
\* block does not.
WakerSignal(g) ==
    /\ waker_pc[g] = "appended"
    /\ waker_pc' = [waker_pc EXCEPT ![g] = "signalled"]
    /\ idle_cond' \in {idle_cond, TRUE}
    /\ pump_efd'  \in {pump_efd,  TRUE}
    /\ UNCHANGED <<sub_list, fp_inflight, hub_pc, hub_timeout, fiber_pc>>

\* L2 step 3: the spawner is done.  Unlike the single-thread blockpool path there
\* is NO bp_inflight decrement gating the M:N backstop -- the heal is the timed
\* poll, armed intrinsically.  fp_inflight is the run's pending count, which
\* keeps the run ALIVE across the whole submit->drain window: the spawner counts
\* the g pending (runloom_mn_pending_inc) BEFORE it publishes it, and it stays
\* counted until the g completes, so runloom_mn_run keeps the hubs looping while
\* it is >0.  We decrement it at RESUME (see HubResume), not here.
WakerDone(g) ==
    /\ waker_pc[g] = "signalled"
    /\ waker_pc' = [waker_pc EXCEPT ![g] = "done"]
    /\ UNCHANGED <<sub_list, idle_cond, pump_efd, fp_inflight, hub_pc, hub_timeout,
                   fiber_pc>>

----------------------------------------------------------------------------
\* ---- Owner hub main loop (runloom_hub_main) ----

\* L3 loop-top: drain a non-empty sub_head under sub_lock; fresh gs go to the
\* hub's deque (or its ready ring, if pinned there).  The UNCONDITIONAL re-drain
\* every loop is what makes sub_head self-healing -- a lost kick costs at most
\* one bounded poll tick.
HubDrain ==
    /\ hub_pc = "loop_top"
    /\ sub_list # {}
    /\ fiber_pc' = [g \in Gs |-> IF g \in sub_list THEN "ready" ELSE fiber_pc[g]]
    /\ sub_list' = {}
    /\ UNCHANGED <<idle_cond, pump_efd, fp_inflight, hub_pc, hub_timeout, waker_pc>>

\* L3 loop-top: pick a drained g and resume it (coro_resume).  in_sub_queue is
\* cleared 1->0 just before the first resume.  fp_inflight (pending) drops when
\* the g completes; the model folds run-to-completion into this step.
HubResume ==
    /\ hub_pc = "loop_top"
    /\ sub_list = {}
    /\ HasReady
    /\ LET nready == Cardinality({g \in Gs : fiber_pc[g] = "ready"}) IN
         /\ fiber_pc' = [g \in Gs |-> IF fiber_pc[g] = "ready" THEN "resumed"
                                                               ELSE fiber_pc[g]]
         /\ fp_inflight' = fp_inflight - nready
    /\ UNCHANGED <<sub_list, idle_cond, pump_efd, hub_pc, hub_timeout, waker_pc>>

\* L3 loop-top with nothing to do: the SNAPSHOT.  The hub drained sub_head empty
\* and latched "nothing to run" (drain found empty + nothing runnable) -- a
\* WakerAppend an instant later is NOT seen before the block.  This is the open
\* edge of the lost-signal window.
HubDrainEmpty ==
    /\ hub_pc = "loop_top"
    /\ sub_list = {}
    /\ ~HasReady
    /\ hub_pc' = "drained_empty"
    /\ UNCHANGED <<sub_list, idle_cond, pump_efd, fp_inflight, hub_timeout,
                   fiber_pc, waker_pc>>

\* fp_inflight is the arming term, analogous to RunloomWake's
\* ForeignParkInflight: the run's pending count, >0 while ANY published fiber has
\* not completed.  runloom_mn_run keeps the hubs looping their bounded poll while
\* it is >0, so the timed re-drain stays armed across the WHOLE submit->drain
\* window even if the last spawner has already finished signalling.
ForeignParkInflight == fp_inflight > 0

\* L3 decide the block timeout.  BoundedPoll=TRUE: a TIMED pump_1ms wait whenever
\* there is still pending work, so the hub re-loops and re-drains within the
\* bound (the heal).  BoundedPoll=FALSE: the regression -- an idle hub that blocks
\* "infinite" (no usable timed wake), the M:N analogue of epoll_wait(-1).
HubDecide ==
    /\ hub_pc = "drained_empty"
    /\ hub_pc' = "decided"
    /\ hub_timeout' = IF (BoundedPoll /\ ForeignParkInflight)
                        THEN "pump_1ms" ELSE "infinite"
    /\ UNCHANGED <<sub_list, idle_cond, pump_efd, fp_inflight, fiber_pc, waker_pc>>

\* L3 enter the block (netpoll_pump(pump_ns) / cond_timedwait_ns in hub_main).
\* The hub is now asleep; the lost-signal window has closed behind it.
HubBlock ==
    /\ hub_pc = "decided"
    /\ hub_pc' = "blocked"
    /\ UNCHANGED <<sub_list, idle_cond, pump_efd, fp_inflight, hub_timeout,
                   fiber_pc, waker_pc>>

\* L4 route 1: a DELIVERED kick (either the wake-pump eventfd OR the idle_cond
\* signal) breaks the wait; consume it and re-loop to the top where HubDrain
\* re-drains sub_head.  This is the fast-path heal (microseconds).
HubKickWake ==
    /\ hub_pc = "blocked"
    /\ AnyKick
    /\ hub_pc' = "loop_top"
    /\ idle_cond' = FALSE                  \* consuming the wait clears both edges
    /\ pump_efd'  = FALSE
    /\ UNCHANGED <<sub_list, fp_inflight, hub_timeout, fiber_pc, waker_pc>>

\* L4 route 2: the pump_1ms timeout elapses (BoundedPoll armed): the timed wait
\* returns 0 events, the hub re-loops and re-drains sub_head -- the M:N self-heal
\* for a LOST kick (runloom_sched_drain.c.inc:186 "they busy-poll ~1ms").
HubPollTimeout ==
    /\ hub_pc = "blocked"
    /\ hub_timeout = "pump_1ms"
    /\ hub_pc' = "loop_top"
    /\ UNCHANGED <<sub_list, idle_cond, pump_efd, fp_inflight, hub_timeout,
                   fiber_pc, waker_pc>>

\* L4 the lasso: an idle hub blocked with NEITHER kick pending AND an unbounded
\* timeout -> nothing re-drains sub_head -> the appended g is stranded forever.
\* Reachable ONLY when BoundedPoll=FALSE (the regression that dropped the timed
\* wait): with BoundedPoll=TRUE the timeout is always pump_1ms while fp_inflight>0,
\* so HubPollTimeout is always enabled and this is unreachable.
HubStuck ==
    /\ hub_pc = "blocked"
    /\ ~AnyKick
    /\ hub_timeout = "infinite"
    /\ UNCHANGED vars

----------------------------------------------------------------------------
Next ==
    \/ \E g \in Gs : WakerAppend(g)
    \/ \E g \in Gs : WakerSignal(g)
    \/ \E g \in Gs : WakerDone(g)
    \/ HubDrain \/ HubResume \/ HubDrainEmpty
    \/ HubDecide \/ HubBlock \/ HubKickWake \/ HubPollTimeout
    \/ HubStuck

\* Weak fairness on every progress action (NOT on HubStuck -- the lasso) so a
\* permanently-blocked hub is a real liveness violation, not an unfair stutter.
Fairness ==
    /\ \A g \in Gs : WF_vars(WakerAppend(g))
    /\ \A g \in Gs : WF_vars(WakerSignal(g))
    /\ \A g \in Gs : WF_vars(WakerDone(g))
    /\ WF_vars(HubDrain) /\ WF_vars(HubResume) /\ WF_vars(HubDrainEmpty)
    /\ WF_vars(HubDecide) /\ WF_vars(HubBlock)
    /\ WF_vars(HubKickWake) /\ WF_vars(HubPollTimeout)

Spec == Init /\ [][Next]_vars /\ Fairness

----------------------------------------------------------------------------
\* SAFETY: the ready/resumed lifecycle is monotone -- a resumed fiber is never
\* still on a hub sub-list, and only an appended fiber is ever readied.  This is
\* the M:N ResumeIsTerminal, identical in spirit to RunloomWake's (a resumed g is
\* never re-queued; the in_sub_queue CAS makes a double publish impossible in
\* the C, this asserts the model never violates it).
ResumeIsTerminal ==
    \A g \in Gs : (fiber_pc[g] = "resumed") => (g \notin sub_list)

\* LIVENESS (the property the bounded poll guarantees): every fiber a spawner
\* appended to a hub's sub-list is EVENTUALLY run -- no lost kick strands it.  Holds under BoundedPoll=TRUE; violated under
\* BoundedPoll=FALSE (the unbounded-block regression).
AllWoken == <>[](\A g \in Gs : fiber_pc[g] = "resumed")
============================================================================
