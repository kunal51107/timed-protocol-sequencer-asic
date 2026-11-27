.od 3
Z 0
Z 1
HIGH 1
HIGH 0
D 40
LO 0
D 26
MARK
LO 1
CALL write
JB bad
CALL write
JB bad
CALL restart
CALL write
JB bad
CALL read
AT 9
LO 0
CALL pulse
CALL read
AT 9
Z 0
CALL pulse
CALL stop
HALT
bad: CALL stop
TRAP 2
write: PULL
LX 8
wb: AT 9
OUTH 0
CALL pulse
DX wb
AT 9
Z 0
CALL pulse
RET
read: LX 8
AT 9
Z 0
rb: CALL pulse
DX rb
PUSH
RET
restart: AT 9
Z 0
HIGH 0
AT 40
Z 1
HIGH 1
D 20
LO 0
D 26
MARK
LO 1
RET
stop: AT 9
LO 0
AT 40
Z 1
HIGH 1
D 20
Z 0
HIGH 0
D 40
RET
pulse: AT 40
Z 1
HIGH 1
D 14
INL 0
PER 60
LO 1
RET
