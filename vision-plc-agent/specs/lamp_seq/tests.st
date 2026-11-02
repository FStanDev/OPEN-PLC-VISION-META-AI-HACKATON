(* Acceptance tests for lamp_seq, mirroring specs/lamp_seq.yaml.
   Single-quoted names: STruC++ v0.7.0 rejects the double-quoted form. *)
SETUP
VAR
    prog : Main;
END_VAR
    prog.start_btn := FALSE;
    prog.stop_btn := FALSE;
END_SETUP

TEST 'start latches all three lamps'
    (* clear any latched state, then pulse start for one scan *)
    prog.stop_btn := TRUE;
    prog();
    prog.stop_btn := FALSE;
    prog.start_btn := TRUE;
    prog();
    prog.start_btn := FALSE;
    prog();
    ASSERT_TRUE(prog.lamp_red);
    ASSERT_TRUE(prog.lamp_green);
    ASSERT_TRUE(prog.lamp_yellow);
END_TEST

TEST 'stop clears all lamps'
    (* latch on first *)
    prog.start_btn := TRUE;
    prog();
    prog.start_btn := FALSE;
    prog();
    ASSERT_TRUE(prog.lamp_red);
    (* press stop: safe state within one scan *)
    prog.stop_btn := TRUE;
    prog();
    ASSERT_FALSE(prog.lamp_red);
    ASSERT_FALSE(prog.lamp_green);
    ASSERT_FALSE(prog.lamp_yellow);
    (* release stop: outputs stay safe *)
    prog.stop_btn := FALSE;
    prog();
    ASSERT_FALSE(prog.lamp_red);
    ASSERT_FALSE(prog.lamp_green);
    ASSERT_FALSE(prog.lamp_yellow);
END_TEST

TEST 'outputs start safe'
    (* no input yet: all lamps off *)
    prog();
    ASSERT_FALSE(prog.lamp_red);
    ASSERT_FALSE(prog.lamp_green);
    ASSERT_FALSE(prog.lamp_yellow);
END_TEST
