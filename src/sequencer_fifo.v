`default_nettype none
// Callers qualify put/pop using the registered occupancy. At count 1..7
// simultaneous accesses select distinct cells and leave occupancy unchanged.
module sequencer_fifo(
    input wire clk, reset, flush, put, pop,
    input wire [7:0] data,
    output wire [7:0] head,
    output reg [3:0] count,
    output wire [3:0] next_count
);
    reg [7:0] cells [0:7];
    reg [2:0] rd, wr;
    integer i;
    assign head = cells[rd];
    assign next_count = flush ? 4'd0 :
                        put == pop ? count :
                        put ? count + 4'd1 : count - 4'd1;

    always @(posedge clk or posedge reset) begin
        if (reset) begin
            for (i = 0; i < 8; i = i + 1) cells[i] <= 0;
        end else if (put) cells[wr] <= data;
    end
    always @(posedge clk or posedge reset) begin
        if (reset) begin
            rd <= 0;
            wr <= 0;
            count <= 0;
        end else if (flush) begin
            rd <= 0;
            wr <= 0;
            count <= 0;
        end else begin
            count <= next_count;
            if (pop) rd <= rd + 1'b1;
            if (put) wr <= wr + 1'b1;
        end
    end
endmodule
`default_nettype wire
