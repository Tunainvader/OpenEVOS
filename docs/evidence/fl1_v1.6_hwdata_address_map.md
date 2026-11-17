# LED board `HW_DATA`: field → persistent-memory address map (L0)

- **Source:**
  - Ghidra 12.2 decompilation of `GetHardwareSpecificData` in `EvosGenIII_LEDBoard64.dll` (SHA-256 `6cc96b78…7fae08`, matches the package inventory);
  - joined with the `HW_DATA__*` enumeration from the same DLL's type library.
- **Encoding:** each field is read with `GetPersistentData` (command `0x51`, read-only) at the listed address. Values are **little-endian**: `int32` for 4-byte fields, IEEE-754 `double` for 8-byte fields.
- **Coverage:** 76 of 118 fields. The other 42 (turret direction, home torque, objective-turret selections, strings, camera calibration tables) use other code paths and are not mapped yet.
- **Labels:** the map describes what the vendor software *expects* (Strongly supported). It says nothing about the values or integrity of our board's memory, which is Unknown until a reviewed L3 read.
- **No values are recorded here.**

| eDataType | Field | Address | Size / type |
|---|---|---|---|
| 0 | HW_DATA__COLORCAMERA__RED_GAIN | 0x141D | 8 B (double) |
| 1 | HW_DATA__COLORCAMERA__GREEN_GAIN | 0x1425 | 8 B (double) |
| 2 | HW_DATA__COLORCAMERA__BLUE_GAIN | 0x142D | 8 B (double) |
| 3 | HW_DATA__EPI_CUBE_1_POS | 0x1435 | 4 B (long) |
| 4 | HW_DATA__EPI_CUBE_2_POS | 0x1439 | 4 B (long) |
| 5 | HW_DATA__EPI_CUBE_3_POS | 0x143D | 4 B (long) |
| 6 | HW_DATA__EPI_CUBE_4_POS | 0x1441 | 4 B (long) |
| 7 | HW_DATA__EPI_MIRROR_POS | 0x1445 | 4 B (long) |
| 8 | HW_DATA__EPI_POS_MIN | 0x1449 | 4 B (long) |
| 9 | HW_DATA__EPI_POS_MAX | 0x144D | 4 B (long) |
| 10 | HW_DATA__X_POS_MIN | 0x1451 | 4 B (long) |
| 11 | HW_DATA__X_POS_MAX | 0x1455 | 4 B (long) |
| 12 | HW_DATA__Y_POS_MIN | 0x1459 | 4 B (long) |
| 13 | HW_DATA__Y_POS_MAX | 0x145D | 4 B (long) |
| 14 | HW_DATA__Z_POS_MIN | 0x1461 | 4 B (long) |
| 15 | HW_DATA__Z_POS_MAX | 0x1465 | 4 B (long) |
| 20 | HW_DATA__X_AXIS_OFFSET | 0x146E | 4 B (long) |
| 21 | HW_DATA__Y_AXIS_OFFSET | 0x1472 | 4 B (long) |
| 22 | HW_DATA__Z_AXIS_OFFSET | 0x1476 | 4 B (long) |
| 23 | HW_DATA__X_AXIS_REGISTRATION | 0x147A | 4 B (long) |
| 24 | HW_DATA__Y_AXIS_REGISTRATION | 0x147E | 4 B (long) |
| 25 | HW_DATA__FOCUS_UNCERTAINTY | 0x1486 | 8 B (double) |
| 63 | HW_DATA__X_AXIS_MM_PER_REVOLUTION | 0x14DE | 8 B (double) |
| 64 | HW_DATA__Y_AXIS_MM_PER_REVOLUTION | 0x14E6 | 8 B (double) |
| 65 | HW_DATA__Z_AXIS_MM_PER_REVOLUTION | 0x14EE | 8 B (double) |
| 66 | HW_DATA__CUBE_PARFOCALITY__TRANSMITTED | 0x14F6 | 4 B (long) |
| 67 | HW_DATA__CUBE_PARFOCALITY__DAPI | 0x14FA | 4 B (long) |
| 68 | HW_DATA__CUBE_PARFOCALITY__GFP | 0x14FE | 4 B (long) |
| 69 | HW_DATA__CUBE_PARFOCALITY__RFP | 0x1502 | 4 B (long) |
| 70 | HW_DATA__CUBE_PARFOCALITY__CFP | 0x1506 | 4 B (long) |
| 71 | HW_DATA__CUBE_PARFOCALITY__YFP | 0x150A | 4 B (long) |
| 72 | HW_DATA__CUBE_PARFOCALITY__TexasRed | 0x150E | 4 B (long) |
| 73 | HW_DATA__CUBE_PARFOCALITY__CY5 | 0x1512 | 4 B (long) |
| 74 | HW_DATA__CUBE_PARFOCALITY__QD525 | 0x1516 | 4 B (long) |
| 75 | HW_DATA__CUBE_PARFOCALITY__QD545 | 0x151A | 4 B (long) |
| 76 | HW_DATA__CUBE_PARFOCALITY__QD565 | 0x151E | 4 B (long) |
| 77 | HW_DATA__CUBE_PARFOCALITY__QD585 | 0x1522 | 4 B (long) |
| 78 | HW_DATA__CUBE_PARFOCALITY__QD605 | 0x1526 | 4 B (long) |
| 79 | HW_DATA__CUBE_PARFOCALITY__QD625 | 0x152A | 4 B (long) |
| 80 | HW_DATA__CUBE_PARFOCALITY__QD655 | 0x152E | 4 B (long) |
| 81 | HW_DATA__CUBE_PARFOCALITY__QD705 | 0x1532 | 4 B (long) |
| 82 | HW_DATA__CUBE_PARFOCALITY__QD800 | 0x1536 | 4 B (long) |
| 83 | HW_DATA__CUBE_PARFOCALITY__LongPass | 0x153A | 4 B (long) |
| 84 | HW_DATA__CUBE_PARFOCALITY__mCherry | 0x153E | 4 B (long) |
| 85 | HW_DATA__CUBE_PARFOCALITY__Custom1 | 0x1542 | 4 B (long) |
| 86 | HW_DATA__CUBE_PARFOCALITY__FITC | 0x1546 | 4 B (long) |
| 87 | HW_DATA__CUBE_PARFOCALITY__TagBFP | 0x154A | 4 B (long) |
| 88 | HW_DATA__CUBE_PARFOCALITY__AcrOrange | 0x154E | 4 B (long) |
| 89 | HW_DATA__CUBE_PARFOCALITY__CY7 | 0x1552 | 4 B (long) |
| 90 | HW_DATA__CUBE_PARFOCALITY__IRD680 | 0x1556 | 4 B (long) |
| 91 | HW_DATA__CUBE_PARFOCALITY__SpOrange | 0x155A | 4 B (long) |
| 92 | HW_DATA__CUBE_PARFOCALITY__SpBlue | 0x155E | 4 B (long) |
| 93 | HW_DATA__CUBE_PARFOCALITY__SpGreen | 0x1562 | 4 B (long) |
| 94 | HW_DATA__CUBE_PARFOCALITY__SpGold | 0x1566 | 4 B (long) |
| 95 | HW_DATA__CUBE_PARFOCALITY__SpRed | 0x156A | 4 B (long) |
| 96 | HW_DATA__CUBE_PARFOCALITY__SpFarRed | 0x156E | 4 B (long) |
| 97 | HW_DATA__CUBE_PARFOCALITY__FluoroGold | 0x1572 | 4 B (long) |
| 98 | HW_DATA__CUBE_PARFOCALITY__LucfrYellow | 0x1576 | 4 B (long) |
| 99 | HW_DATA__CUBE_PARFOCALITY__CfpYfpEm | 0x157A | 4 B (long) |
| 100 | HW_DATA__CUBE_PARFOCALITY__ChlorophyllB | 0x157E | 4 B (long) |
| 101 | HW_DATA__CUBE_PARFOCALITY__Proto5 | 0x1582 | 4 B (long) |
| 102 | HW_DATA__CUBE_PARFOCALITY__Proto6 | 0x1586 | 4 B (long) |
| 103 | HW_DATA__CUBE_PARFOCALITY__Proto7 | 0x158A | 4 B (long) |
| 104 | HW_DATA__CUBE_PARFOCALITY__Proto8 | 0x158E | 4 B (long) |
| 105 | HW_DATA__CUBE_PARFOCALITY__Proto9 | 0x1592 | 4 B (long) |
| 106 | HW_DATA__CUBE_PARFOCALITY__Proto10 | 0x1596 | 4 B (long) |
| 107 | HW_DATA__CUBE_PARFOCALITY__Proto11 | 0x159A | 4 B (long) |
| 108 | HW_DATA__CUBE_PARFOCALITY__Proto12 | 0x159E | 4 B (long) |
| 109 | HW_DATA__CUBE_PARFOCALITY__Proto13 | 0x15A2 | 4 B (long) |
| 110 | HW_DATA__CUBE_PARFOCALITY__Proto14 | 0x15A6 | 4 B (long) |
| 111 | HW_DATA__CUBE_PARFOCALITY__Proto15 | 0x15AA | 4 B (long) |
| 112 | HW_DATA__CUBE_PARFOCALITY__Proto16 | 0x15AE | 4 B (long) |
| 113 | HW_DATA__CUBE_PARFOCALITY__Proto17 | 0x15B2 | 4 B (long) |
| 114 | HW_DATA__CUBE_PARFOCALITY__White | 0x15B6 | 4 B (long) |
| 115 | HW_DATA__CUBE_PARFOCALITY__Proto19 | 0x15BA | 4 B (long) |
| 116 | HW_DATA__CUBE_PARFOCALITY__Proto20 | 0x15BE | 4 B (long) |
