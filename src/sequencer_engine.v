`default_nettype none
// Bring-up subset for the UART TX image. The host, store and FIFOs are full size.
module sequencer_engine(
    input wire clk, reset, clear_exec, run,
    input wire [15:0] word,
    input wire [11:0] levels, history,
    input wire [7:0] od, tx_head,
    input wire [1:0] cfg,
    input wire tx_empty, rx_full,
    output reg [6:0] pc,
    output reg [7:0] p_data, p_oe,
    output wire [7:0] r,
    output wire [3:0] o,
    output wire pull, push, halt, fatal,
    output wire [7:0] error_bits
);
    reg [7:0] t;
    reg [11:0] delay_left;
    wire [11:0] remaining = delay_left == 0 ? word[11:0] : delay_left - 12'd1;
    wire delay_op = word[15:12] == 1;
    wire jump = word[15:7] == 9'h0c0;
    wire write_pin = word == 16'h3001 || word == 16'h3002 || word == 16'h3003;
    wire value = word == 16'h3002 || (word == 16'h3003 && t[0]);
    wire underflow = run && word == 4 && tx_empty && !cfg[0];
    wire illegal = run && !(word == 0 || word == 4 || delay_op || jump || write_pin);
    assign error_bits = {2'b0,illegal,1'b0,underflow,3'b0};
    assign fatal = underflow || illegal;
    assign halt = run && word == 0;
    assign pull = run && word == 4 && !tx_empty;
    assign {push,r,o} = 13'd0;
    wire _unused = &{1'b0,levels,history,od[7:1],cfg[1],rx_full};
    always @(posedge clk or posedge reset) begin
        if (reset) begin
            {pc,p_data,p_oe,t,delay_left} <= 0;
        end else if (clear_exec) begin
            {pc,p_data,p_oe,t,delay_left} <= 0;
        end else if (fatal) begin
            {p_data,p_oe} <= 0;
        end else if (run && !halt) begin
            if (delay_op) delay_left <= remaining;
            if (jump) pc <= word[6:0];
            else if (!(delay_op && remaining != 0) && !(word == 4 && tx_empty))
                pc <= pc + 7'd1;
            if (pull) t <= tx_head;
            if (write_pin) begin
                p_data[0] <= value && !od[0];
                p_oe[0] <= !(value && od[0]);
                if (word == 16'h3003) t <= {1'b0,t[7:1]};
            end
        end
    end
endmodule
`default_nettype wire
