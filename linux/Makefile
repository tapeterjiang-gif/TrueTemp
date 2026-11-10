CC ?= cc
CFLAGS ?= -O2 -std=c11 -Wall -Wextra
LDFLAGS ?=
LDLIBS ?= -lm

TARGET = truetemp_infer
OBJS = truetemp.o truetemp_infer.o
SNOW2S_MODEL_DIR ?= ../models/snow2s

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $(OBJS) $(LDLIBS)

truetemp.o: truetemp.c truetemp.h truetemp_model_data.h
	$(CC) $(CFLAGS) -c truetemp.c -o $@

truetemp_infer.o: truetemp_infer.c truetemp.h
	$(CC) $(CFLAGS) -c truetemp_infer.c -o $@

snow2s: truetemp_infer_snow2s

truetemp_infer_snow2s: truetemp_snow2s.o truetemp_infer_snow2s.o
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

truetemp_snow2s.o: truetemp.c truetemp.h truetemp_snow2s_model_data.h
	$(CC) $(CFLAGS) -DTRUETEMP_SNOW2S -c $< -o $@

truetemp_infer_snow2s.o: truetemp_infer.c truetemp.h
	$(CC) $(CFLAGS) -DTRUETEMP_SNOW2S -c $< -o $@

truetemp_snow2s_model_data.h: $(SNOW2S_MODEL_DIR)/model_temperature.json $(SNOW2S_MODEL_DIR)/model_vapor_pressure.json ../gen_c_model.py infer_truetemp.py
	python3 ../gen_c_model.py --linux --snow2s --model-dir "$(SNOW2S_MODEL_DIR)" --output "$@"

model:
	cd .. && python3 gen_c_model.py --linux

clean:
	rm -f $(TARGET) $(OBJS) truetemp_infer_snow2s truetemp_snow2s.o truetemp_infer_snow2s.o

.PHONY: all model clean snow2s
