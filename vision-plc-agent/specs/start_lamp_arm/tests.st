(* Acceptance tests for start_lamp_arm, mirroring specs/start_lamp_arm.yaml.
   Single-quoted names: STruC++ v0.7.0 rejects the double-quoted form.
   Tests run in file order; each of the first two drives the program to
   a known state first, so they pass whether or not the SETUP instance
   is shared between tests. *)
SETUP
VAR
    prog : Main;
END_VAR
    prog.start_btn := FALSE;
    prog.stop_btn := FALSE;
END_SETUP

TEST 'start latches lamp and moves arm'
    (* clear any latched state, then pulse start for one scan *)
    prog.stop_btn := TRUE;
    prog();
    prog.stop_btn := FALSE;
    prog.start_btn := TRUE;
    prog();
    prog.start_btn := FALSE;
    prog();
    ASSERT_TRUE(prog.lamp_red);
    ASSERT_EQ(prog.arm_angle, 90);
END_TEST

TEST 'stop clears lamp and homes arm'
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
    ASSERT_EQ(prog.arm_angle, 0);
    (* release stop: outputs stay safe *)
    prog.stop_btn := FALSE;
    prog();
    ASSERT_FALSE(prog.lamp_red);
    ASSERT_EQ(prog.arm_angle, 0);
END_TEST

TEST 'outputs start safe'
    (* no input yet: lamp off, arm home *)
    prog();
    ASSERT_FALSE(prog.lamp_red);
    ASSERT_EQ(prog.arm_angle, 0);
END_TEST
