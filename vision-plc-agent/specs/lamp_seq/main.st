(* lamp_seq: start latches all three lamps on; stop drives them off.
   Hardware cross-check fixture: verifies each lamp channel and the
   vision box assignment after the start_lamp_arm physical-fault findings.
   The arm is intentionally not driven here. *)
PROGRAM Main
VAR
    start_btn AT %IX0.0 : BOOL;
    stop_btn AT %IX0.1 : BOOL;
    lamp_red AT %QX0.0 : BOOL;
    lamp_green AT %QX0.1 : BOOL;
    lamp_yellow AT %QX0.2 : BOOL;
    start_edge : R_TRIG; (* rising edge of the start button *)
    stop_edge : R_TRIG;  (* rising edge of the stop button *)
    run_latch : SR;      (* set by start, reset by stop *)
END_VAR

    (* Edge-detect both buttons; never act on a level twice. *)
    start_edge(CLK := start_btn);
    stop_edge(CLK := stop_btn);

    (* Latch the run state; stop wins through the reset input. *)
    run_latch(S1 := start_edge.Q, R := stop_edge.Q);

    (* The stop level forces the safe state in the same scan. *)
    lamp_red := run_latch.Q1 AND NOT stop_btn;
    lamp_green := run_latch.Q1 AND NOT stop_btn;
    lamp_yellow := run_latch.Q1 AND NOT stop_btn;
END_PROGRAM
