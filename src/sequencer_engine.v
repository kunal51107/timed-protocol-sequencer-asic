`default_nettype none
// Single instruction per rising edge. All event outputs are qualified by run.
module sequencer_engine(
    input wire clk, reset, clear_exec, run,
    input wire [15:0] word,
    input wire [11:0] levels, history,
    input wire [7:0] od, tx_head,
    input wire [1:0] cfg,
    input wire tx_empty, rx_full,
    output reg [6:0] pc,
    output reg [7:0] p_data, p_oe, r,
    output reg [3:0] o,
    output wire pull, push, halt, fatal,
    output reg [7:0] error_bits
);
    reg [7:0] t, x, y, v, e;
    reg b, q;
    reg [6:0] stack [0:1];
    reg [1:0] depth;
    reg [11:0] age, delay_left;
    wire [11:0] elapsed = (&age) ? age : age + 12'd1;
    wire [11:0] sub = word[11:0];
    wire [3:0] op = word[15:12], field = word[11:8];
    wire [3:0] pin_id = word[7:4], action = word[3:0];
    wire [7:0] imm = word[7:0];
    wire [6:0] dest = word[6:0], sequential = pc + 7'd1;
    wire sample = levels[pin_id], old = history[pin_id];
    wire [11:0] remaining = delay_left == 0 ? sub : delay_left - 12'd1;
    reg branch, sense_ok, drive, legal;

    always @* begin
        case (word[11:7])
            0: branch = 1;
            1: branch = b;
            2: branch = !b;
            3: branch = q;
            4: branch = !q;
            5: branch = t[0];
            6: branch = t[7];
            7: branch = tx_empty;
            8: branch = rx_full;
            9: branch = x == 0;
            10: branch = y == 0;
            default: branch = 0;
        endcase
        case (action)
            0: sense_ok = !sample;
            1: sense_ok = sample;
            2: sense_ok = sample && !old;
            3: sense_ok = !sample && old;
            default: sense_ok = 1;
        endcase
        case (action)
            2: drive = 1;
            3: drive = t[0];
            4: drive = t[7];
            5: drive = q;
            6: drive = b;
            default: drive = 0;
        endcase
        case (op)
            0: legal = sub <= 11;
            1,2,11: legal = 1;
            3,4: legal = field == 0 && pin_id < 12 && action < 7 &&
                         !(op == 3 && pin_id >= 8 && action == 0);
            5: legal = field < 4;
            6: legal = word[11:7] <= 10;
            7,9: legal = field == 0;
            8: legal = word[11:7] == 0;
            10: legal = field < 2 || sub == 12'h200;
            default: legal = 0;
        endcase
        error_bits = 0;
        if (run) begin
            if (!legal) error_bits[5] = 1;
            else if (op == 9) error_bits[7] = 1;
            else if ((op == 8 && depth == 2) || (word == 3 && depth == 0))
                error_bits[6] = 1;
            else if (word == 4 && tx_empty && !cfg[0]) error_bits[3] = 1;
            else if (word == 5 && rx_full) error_bits[4] = 1;
        end
    end
    assign fatal = (|error_bits[7:5]) || (error_bits[4] && cfg[1]) || error_bits[3];
    assign pull = run && word == 4 && !tx_empty;
    assign push = run && word == 5 && !rx_full;
    assign halt = run && word == 0;

    reg [6:0] next_pc;
    always @* begin
        next_pc = sequential;
        case (op)
            0: if (word == 3) next_pc = stack[depth == 2];
               else if (word == 4 && tx_empty) next_pc = pc;
            1: if (remaining != 0) next_pc = pc;
            2,11: if (elapsed < sub) next_pc = pc;
            4: if (!sense_ok) next_pc = pc;
            6: if (branch) next_pc = dest;
            7: if ((word[7] ? y : x) != 1) next_pc = dest;
            8: next_pc = dest;
            default: next_pc = sequential;
        endcase
    end

    task clear_state;
        begin
            pc <= 0;
            {t,r,x,y,b,q,v,e} <= 0;
            {p_data,p_oe,o} <= 0;
            {age,delay_left,depth} <= 0;
            stack[0] <= 0;
            stack[1] <= 0;
        end
    endtask

    always @(posedge clk or posedge reset) begin
        if (reset) clear_state;
        else if (clear_exec) clear_state;
        else if (run) begin
            age <= elapsed;
            if (fatal) begin
                {p_data,p_oe,o} <= 0;
            end else if (!halt) begin
                pc <= next_pc;
                case (op)
                    0: case (sub)
                        2: age <= 0;
                        3: depth <= depth - 1'b1;
                        4: if (pull) t <= tx_head;
                        6: r <= 0;
                        7: t <= r;
                        8: r <= t;
                        9: q <= 0;
                        10: q <= 1;
                        11: q <= b;
                        default: ;
                    endcase
                    1: delay_left <= remaining;
                    3: begin
                        if (pin_id[3]) o[pin_id[1:0]] <= drive;
                        else begin
                            p_data[pin_id[2:0]] <= drive && !od[pin_id[2:0]];
                            p_oe[pin_id[2:0]] <= action != 0 && !(od[pin_id[2:0]] && drive);
                        end
                        if (action == 3) t <= {1'b0,t[7:1]};
                        if (action == 4) t <= {t[6:0],1'b0};
                        if (action == 3 || action == 4) q <= q ^ drive;
                    end
                    4: begin
                        if (action >= 4) b <= sample;
                        if (action == 5) r <= {r[6:0],sample};
                        if (action == 6) r <= {sample,r[7:1]};
                        if (action == 5 || action == 6) q <= q ^ sample;
                    end
                    5: case (field[1:0])
                        0: t <= imm;
                        1: r <= imm;
                        2: x <= imm;
                        3: y <= imm;
                    endcase
                    7: if (word[7]) y <= y - 1'b1; else x <= x - 1'b1;
                    8: begin
                        stack[depth[0]] <= sequential;
                        depth <= depth + 1'b1;
                    end
                    10: case (field)
                        0: v <= imm;
                        1: e <= imm;
                        2: begin
                            p_data <= v & ~od;
                            p_oe <= e & ~(od & v);
                        end
                        default: ;
                    endcase
                    11: if (elapsed >= sub) age <= 0;
                    default: ;
                endcase
            end
        end
    end
endmodule
`default_nettype wire
