CC ?= cc
CFLAGS ?= -O2 -std=c11 -Wall -Wextra
LDFLAGS ?=
LDLIBS ?= -lm

TARGET = truetemp_infer
OBJS = truetemp.o truetemp_infer.o

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $(OBJS) $(LDLIBS)

truetemp.o: truetemp.c truetemp.h truetemp_model_data.h
	$(CC) $(CFLAGS) -c truetemp.c -o $@

truetemp_infer.o: truetemp_infer.c truetemp.h
	$(CC) $(CFLAGS) -c truetemp_infer.c -o $@

model:
	python3 ../gen_c_model.py --linux

clean:
	rm -f $(TARGET) $(OBJS)

.PHONY: all model clean
