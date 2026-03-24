TOY_SCALAR_PATH ?= $(CURDIR)

RTL_COMPILE_OUTPUT 				= $(TOY_SCALAR_PATH)/work/rtl_compile
RTL_COMPILE_OUTPUT_NOICACHE 	= $(TOY_SCALAR_PATH)/work/rtl_compile_noicache
LOG_OUTPUT_DIR		= $(TOY_SCALAR_PATH)/logs
RV_SINGLE_ISA		= rv32ui-p-sw
LSU_DEBUG_ARGS		?= +LSU_PIPE_LOG=$(LOG_OUTPUT_DIR)/lsu_pipe.log +STQ_ENTRY_WR=$(LOG_OUTPUT_DIR)/v_stq_repetitive_write.log +STQ_WD=$(LOG_OUTPUT_DIR)/stq_writedown.log  +COMMIT_STQ=$(LOG_OUTPUT_DIR)/stq_commit.log +LSU_HZD=$(LOG_OUTPUT_DIR)/lsu_hzd.log +LDQ=$(LOG_OUTPUT_DIR)/ldq_trace.log +STQ_ENTRY_ACK_STATE=$(LOG_OUTPUT_DIR)/v_stq_discontinuous_ack.log +STQ_ENTRY_HSK_TRACE=$(LOG_OUTPUT_DIR)/v_stq_entry_hsk_trace.log +RENAME_BP_TRACE=$(LOG_OUTPUT_DIR)/lsu_q_bp_rename.log +ISSUE_REALLOC_LOG=$(LOG_OUTPUT_DIR)/issue_realloc.log +STQ_SLICE_TRACE=$(LOG_OUTPUT_DIR)/stq_slice_trace.log +ISSUE_LSU_BP_TRACE=$(LOG_OUTPUT_DIR)/issue_lsu_bp.log +ISSUE_LSU_BP_TRACE_EN +LOAD_TRACE_EN +LOAD_TRACE=$(LOG_OUTPUT_DIR)/commit_load_trace.log +LS_CHECK_LOG=$(LOG_OUTPUT_DIR)/ls_check.log

TIMESTAMP 			= $(shell TZ='Asia/Shanghai' date +%Y%m%d_%H%M_%S)
GIT_REVISION 		= $(shell git show -s --pretty=format:%h)
.PHONY: compile lint

compile:
	mkdir -p $(RTL_COMPILE_OUTPUT)
	cd $(RTL_COMPILE_OUTPUT) ;vcs -kdb -full64 -debug_access -sverilog -f $(SIM_FILELIST) +lint=PCWM +lint=TFIPC-L +define+TOY_SIM
compile_debug:
	mkdir -p $(RTL_COMPILE_OUTPUT)
	cd $(RTL_COMPILE_OUTPUT) ;vcs -kdb -full64 -debug_access -sverilog -f $(SIM_FILELIST) +lint=PCWM +lint=TFIPC-L +define+TOY_SIM+DEBUG

# wsl compile
comp:
	mkdir -p $(RTL_COMPILE_OUTPUT)
	cd $(RTL_COMPILE_OUTPUT) ;vcs -full64 -cpp g++-4.8 -cc gcc-4.8 -LDFLAGS -Wl,--no-as-needed -kdb -lca -full64 -debug_access -sverilog -f $(SIM_FILELIST) +lint=PCWM +lint=TFIPC-L +define+TOY_SIM+WSL -l comp.log

lint:
	fde -file $(TOY_SCALAR_PATH)/qc/lint.tcl -flow lint

isa:
	cd ./rv_isa_test/build ;ctest -j64

dhry_debug:
	${RTL_COMPILE_OUTPUT}/simv +HEX=$(TOY_SCALAR_PATH)/rv_isa_test/dhry/dhrystone_itcm.hex +DATA_HEX=$(TOY_SCALAR_PATH)/rv_isa_test/dhry/dhrystone_dtcm.hex +TIMEOUT=200000 +PC=pc_trace.log +REG_TRACE=reg_trace.log +FETCH=fetch.log +L1D_OUTPUT_DIR=./L1D_output $(LSU_DEBUG_ARGS) +WAVE | tee benchmark_output/dhry/$(TIMESTAMP)_$(GIT_REVISION).log

dhry_1000_debug:
	${RTL_COMPILE_OUTPUT}/simv +HEX=$(TOY_SCALAR_PATH)/rv_isa_test/dhry/dhrystone_itcm1000.hex +DATA_HEX=$(TOY_SCALAR_PATH)/rv_isa_test/dhry/dhrystone_dtcm1000.hex +TIMEOUT=2000000 +REG_TRACE=reg_trace.log $(LSU_DEBUG_ARGS) +WAVE |tee benchmark_output/dhry/$(TIMESTAMP)_$(GIT_REVISION).log

dhry:
	${RTL_COMPILE_OUTPUT}/simv +HEX=$(TOY_SCALAR_PATH)/rv_isa_test/dhry/dhrystone_itcm.hex +DATA_HEX=$(TOY_SCALAR_PATH)/rv_isa_test/dhry/dhrystone_dtcm.hex +TIMEOUT=200000 $(LSU_DEBUG_ARGS) | tee benchmark_output/dhry/$(TIMESTAMP)_$(GIT_REVISION).log

dhry_1000:
	${RTL_COMPILE_OUTPUT}/simv +HEX=$(TOY_SCALAR_PATH)/rv_isa_test/dhry/dhrystone_itcm1000.hex +DATA_HEX=$(TOY_SCALAR_PATH)/rv_isa_test/dhry/dhrystone_dtcm1000.hex +TIMEOUT=2000000 $(LSU_DEBUG_ARGS) |tee benchmark_output/dhry/$(TIMESTAMP)_$(GIT_REVISION).log

cm:
	${RTL_COMPILE_OUTPUT}/simv +HEX=$(TOY_SCALAR_PATH)/rv_isa_test/cm/coremark_itcm.hex +DATA_HEX=$(TOY_SCALAR_PATH)/rv_isa_test/cm/coremark_dtcm.hex +TIMEOUT=0 | tee benchmark_output/cm/$(TIMESTAMP)_$(GIT_REVISION).log

cm_debug:
	${RTL_COMPILE_OUTPUT}/simv +HEX=$(TOY_SCALAR_PATH)/rv_isa_test/cm/coremark_itcm.hex +DATA_HEX=$(TOY_SCALAR_PATH)/rv_isa_test/cm/coremark_dtcm.hex +TIMEOUT=400000 +REG_TRACE=reg_trace.log $(LSU_DEBUG_ARGS) +WAVE | tee benchmark_output/cm/$(TIMESTAMP)_$(GIT_REVISION).log

verdi:
	verdi -sv -f $(SIM_FILELIST) -ssf wave.fsdb -dbdir $(RTL_COMPILE_OUTPUT)/simv.daidir

sim_single_isa:
	mkdir -p L1D_output 
	mkdir -p $(LOG_OUTPUT_DIR)
	${RTL_COMPILE_OUTPUT}/simv "-exitstatus" "+HEX=${RV_TEST_PATH}/isa/${RV_SINGLE_ISA}_itcm.hex" "+DATA_HEX=${RV_TEST_PATH}/isa/${RV_SINGLE_ISA}_data.hex" "+TIMEOUT=12000" "+WAVE" "+PC=pc_trace.log" "+REG_TRACE=reg_trace.log" "+FETCH=fetch.log" "+L1D_OUTPUT_DIR=./L1D_output" -l sim.log ${LSU_DEBUG_ARGS}

show_isa:
	code ${RV_TEST_PATH}/isa/${RV_SINGLE_ISA}.dump

reg_check:
# 	python3 find_diff.py reg_trace.log ${OOO_LSU}/reg_trace.log 3
	python3 find_diff.py reg_trace.log ${DCACHE}/reg_trace.log 3

all:compile sim_single_isa reg_check