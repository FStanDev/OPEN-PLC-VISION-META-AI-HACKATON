(* start_lamp_arm: start latches the red lamp and moves the arm to 90 deg.
   Stop drives all outputs safe (lamp off, arm to 0) within one scan. *)
PROGRAM Main
VAR
    start_btn AT %IX0.0 : BOOL;
    stop_btn AT %IX0.1 : BOOL;
    lamp_red AT %QX0.0 : BOOL;
    arm_angle AT %QW0 : INT;
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
    IF stop_btn THEN
        arm_angle := 0;
    ELSIF run_latch.Q1 THEN
        arm_angle := 90;
    ELSE
        arm_angle := 0;
    END_IF;
END_PROGRAM
